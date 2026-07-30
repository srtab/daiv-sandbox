from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from docker.errors import NotFound

from daiv_sandbox.config import settings
from daiv_sandbox.egress.manager import EgressProxyManager
from daiv_sandbox.locks import SessionBusyError
from daiv_sandbox.sessions import (
    DAIV_SANDBOX_TYPE_LABEL,
    TYPE_CMD_EXECUTOR,
    TYPE_EGRESS_NETWORK,
    TYPE_EGRESS_PROXY,
    SandboxDockerSession,
    egress_token,
)

logger = logging.getLogger("daiv_sandbox")

_REAPER_LEADER_KEY = "daiv-sandbox:reaper-leader"


def _parse_docker_timestamp(value: str) -> datetime | None:
    """Parse a Docker RFC3339 timestamp (e.g. ``State.FinishedAt``) to an aware UTC datetime.

    Docker emits up to 9 fractional digits and a trailing ``Z`` (e.g.
    ``2026-06-01T12:34:56.123456789Z``). ``datetime.fromisoformat`` rejects >6 fractional digits,
    so truncate to microseconds. The zero value ``0001-01-01T00:00:00Z`` means "not set" (e.g. a
    still-running container) and maps to ``None``; unparseable input also maps to ``None``.
    """
    if not value or value.startswith("0001-01-01"):
        return None
    text = value[:-1] if value.endswith("Z") else value
    if "." in text:
        head, frac = text.split(".", 1)
        text = f"{head}.{frac[:6]}"
    try:
        return datetime.fromisoformat(text).replace(tzinfo=UTC)
    except ValueError:
        return None


def _list_stopped_sandbox_containers(client) -> list:
    """Return all sandbox cmd-executor containers that are not currently running.

    Filters by label, then drops running containers in Python so every non-running state
    (``exited`` from a clean stop, a crash/OOM, or ``dead``) is collected.
    """
    containers = client.containers.list(all=True, filters={"label": f"{DAIV_SANDBOX_TYPE_LABEL}={TYPE_CMD_EXECUTOR}"})
    return [c for c in containers if getattr(c, "status", None) != "running"]


def _list_running_sandbox_containers(client) -> list:
    """Return all sandbox cmd-executor containers that are currently running."""
    containers = client.containers.list(filters={"label": f"{DAIV_SANDBOX_TYPE_LABEL}={TYPE_CMD_EXECUTOR}"})
    return [c for c in containers if getattr(c, "status", None) == "running"]


async def _still_stopped(container) -> tuple[bool, str]:
    """Default removal precondition: proceed only while the container is still not running."""
    if getattr(container, "status", None) == "running":
        return False, "is running again"
    return True, ""


def _make_still_idle(activity, idle_seconds: int):
    """Build the idle-path removal precondition: still running AND still positively confirmed idle.

    Fails closed on an unknown activity read: ``last_seen`` returns None for a Redis fault or a record
    cleared by a concurrent close, and the outer sweep treats None as seed-and-wait, so this
    destructive branch must not read it as "idle".

    Re-checks the run state too, which the default ``_still_stopped`` would otherwise have covered: a
    warm (non-force) DELETE landing during the lock wait stops the container, and force-removing it
    here would bypass the grace window and LRU cap that preserve its writable layer for reuse.
    """

    async def _still_idle(container) -> tuple[bool, str]:
        if getattr(container, "status", None) != "running":
            return False, "is no longer running; leaving it to the stopped-container sweep"
        current = await activity.last_seen(container.id)
        if current is None:
            return False, "activity record became unknown"
        if (datetime.now(UTC) - current).total_seconds() < idle_seconds:
            return False, "was touched again"
        return True, ""

    return _still_idle


async def _remove_guarded(container, lock_manager, *, precondition=_still_stopped) -> bool:
    """Force-remove *container* while holding its per-session lock.

    Re-reads the container state under the lock and re-evaluates *precondition* before removing it:
    the sweep's list-time decision may be stale by the time the lock is acquired (a TOCTOU). On the
    stopped path a request (e.g. a GET/run restart-on-access) may have warmed the container; on the
    idle path a request may have touched the session. The per-session lock serializes against
    in-flight requests, but it does not by itself prevent a change that landed just before the lock
    was acquired — hence the re-check.

    Returns True if the container was removed (or had already vanished), False if the session was
    busy or the precondition no longer holds (skip and let a later sweep retry).
    """
    try:
        async with lock_manager.acquire(container.id):
            await asyncio.to_thread(container.reload)
            proceed, reason = await precondition(container)
            if not proceed:
                logger.info("Reaper: container %s %s; skipping removal", container.id, reason)
                return False
            # v=True: a caller-supplied base_image may declare VOLUME, which would otherwise leak one
            # anonymous volume per session. Only anonymous volumes are removed; there are no binds.
            await asyncio.to_thread(container.remove, force=True, v=True)
            if token := egress_token(container):
                try:
                    manager = EgressProxyManager(SandboxDockerSession._get_shared_client())
                    await asyncio.to_thread(manager.teardown, token)
                except Exception:
                    # The container is gone, so its token is now unreferenced and _reap_orphan_triads
                    # finishes the job. Removal succeeded — do not report it as a failure.
                    logger.exception(
                        "Reaper: removed container %s but failed to tear down egress triad %s", container.id, token
                    )
    except SessionBusyError:
        logger.info("Reaper: session %s busy; skipping this tick", container.id)
        return False
    except NotFound:
        return True
    except Exception:
        logger.exception("Reaper: failed to remove container %s", container.id)
        return False
    else:
        logger.info("Reaper: removed container %s", container.id)
        return True


async def _reap_idle_running_sessions(client, lock_manager, activity, *, now, idle_seconds: int) -> None:
    """Remove running cmd-executors that no request has touched for *idle_seconds*.

    This is the only path that reclaims a session whose ``close_session`` never arrived (worker
    killed, stack redeployed mid-run, crashed or abandoned job). Without it such a container — and
    the egress proxy that its still-live token shields from the orphan-triad sweep — stays up
    forever, because every other sweep here considers only *stopped* containers.

    Safety rests on two independent guards, because ``COMMAND_TIMEOUT`` defaults to unbounded and no
    idle window can be proven longer than the longest legitimate command. First, the per-session lock:
    a command holds it for its whole duration, so the reaper's own ``acquire`` raises
    ``SessionBusyError`` and skips the container mid-run however stale its record looks. Second, the
    record is stamped on operation *exit* as well as entry (see ``_workspace_executor``), so a command
    that outlasts the window does not leave a reapable record behind when it releases the lock. The
    lock alone is not enough: ``RedisSessionLockManager`` stops refreshing after a failed reacquire,
    and the lock TTL is far shorter than this window.

    A session whose activity is *unknown* is seeded rather than reaped. That costs a full idle window
    but makes the unknown state safe in both directions: sessions predating this feature and live
    sessions whose record was lost to a Redis flush each get a fresh window, during which normal
    traffic re-touches them. Requires an enabled tracker — see ``NoopSessionActivityTracker``.
    """
    if idle_seconds <= 0 or not activity.enabled:
        return

    still_idle = _make_still_idle(activity, idle_seconds)
    containers = await asyncio.to_thread(_list_running_sandbox_containers, client)
    for container in containers:
        try:
            last_seen = await activity.last_seen(container.id)
        except Exception:
            # Isolate per container, as _reap_orphan_triads does: one unreadable record must not starve
            # the remaining sessions or the orphan-triad sweep that runs after this one.
            logger.exception("Reaper: failed to read activity for running session %s", container.id)
            continue
        if last_seen is None:
            logger.info("Reaper: activity record unknown for running session %s; seeding it", container.id)
            await activity.touch(container.id)
            continue
        idle_for = (now - last_seen).total_seconds()
        if idle_for < idle_seconds:
            continue

        # debug, not info: the lock/precondition may well skip this container, and an info line here
        # would claim a removal on every tick for the whole duration of a long-running command.
        logger.debug("Reaper: running session %s idle for %.0fs; attempting removal", container.id, idle_for)
        if await _remove_guarded(container, lock_manager, precondition=still_idle):
            logger.info("Reaper: reclaimed idle running session %s (idle %.0fs)", container.id, idle_for)
            await activity.forget(container.id)


async def _reap_once(
    client, lock_manager, *, now, grace_seconds: int, max_stopped: int, activity, idle_seconds: int
) -> None:
    """One sweep: remove stopped containers older than the grace window, then LRU-evict any beyond
    the count cap (oldest ``FinishedAt`` first), then reclaim idle running sessions and orphan
    triads. Containers with no parseable ``FinishedAt`` are kept and treated as newest for cap
    ordering."""
    containers = await asyncio.to_thread(_list_stopped_sandbox_containers, client)

    survivors: list[tuple[object, datetime | None]] = []
    for container in containers:
        finished = _parse_docker_timestamp((container.attrs or {}).get("State", {}).get("FinishedAt", ""))
        if finished is not None and (now - finished).total_seconds() >= grace_seconds:
            await _remove_guarded(container, lock_manager)
        else:
            survivors.append((container, finished))

    if max_stopped >= 0 and len(survivors) > max_stopped:
        # Oldest first; unknown FinishedAt sorts as "now" (kept last, i.e. not evicted first).
        survivors.sort(key=lambda item: item[1] or now)
        for container, _finished in survivors[: len(survivors) - max_stopped]:
            await _remove_guarded(container, lock_manager)

    # Before the triad sweep: a half-failed teardown here leaves the token unreferenced, so the backstop
    # below reclaims it — once the triad outlives grace_seconds, not necessarily in this tick.
    await _reap_idle_running_sessions(client, lock_manager, activity, now=now, idle_seconds=idle_seconds)

    # Sweep orphan triads unconditionally, NOT gated on settings.egress_enabled: an operator who disables
    # egress (removes the CA files) while triads from earlier sessions still exist would otherwise strand
    # those proxy containers + internal networks forever. It is a cheap no-op when nothing is labelled.
    await _reap_orphan_triads(client, now=now, grace_seconds=grace_seconds)


async def _reap_orphan_triads(client, *, now, grace_seconds: int) -> None:
    """Tear down egress triads (sidecar proxy + internal network) whose token no cmd-executor carries.

    A backstop for the rare paths that drop the sandbox<->triad link without tearing the triad down
    (a crash mid-start, or a swallowed teardown error on a force-close): the normal teardown happens in
    close_session and ``_remove_guarded`` via the surviving cmd-executor, so an orphan only appears when
    that container is already gone. There is no per-session lock keyed by the egress token (start_session
    holds none), so we guard the mid-start TOCTOU by age: a triad whose newest resource is younger than
    the grace window is left for a later sweep, since a slow start (e.g. a long image pull) may not have
    created/labelled its cmd-executor yet. ``teardown(token)`` removes both the proxy and the network, so
    a network-only or proxy-only remnant is reclaimed by token either way."""
    proxies = await asyncio.to_thread(
        client.containers.list, all=True, filters={"label": f"{DAIV_SANDBOX_TYPE_LABEL}={TYPE_EGRESS_PROXY}"}
    )
    networks = await asyncio.to_thread(
        client.networks.list, filters={"label": f"{DAIV_SANDBOX_TYPE_LABEL}={TYPE_EGRESS_NETWORK}"}
    )
    cmd_executors = await asyncio.to_thread(
        client.containers.list, all=True, filters={"label": f"{DAIV_SANDBOX_TYPE_LABEL}={TYPE_CMD_EXECUTOR}"}
    )
    live_tokens = {tok for tok in (egress_token(c) for c in cmd_executors) if tok}

    # token -> newest creation time across its resources; an unknown creation time maps to `now` so a
    # resource we can't age is treated as just-created (never reaped) rather than as ancient.
    newest_created: dict[str, datetime] = {}
    for resource in [*proxies, *networks]:
        token = egress_token(resource)
        if not token:
            continue
        created = _parse_docker_timestamp((getattr(resource, "attrs", None) or {}).get("Created", "")) or now
        newest_created[token] = max(newest_created.get(token, created), created)

    manager = None
    for token, created in newest_created.items():
        if token in live_tokens:
            continue
        if (now - created).total_seconds() < grace_seconds:
            continue  # too new (possibly an in-flight start) — let a later sweep reclaim it if it persists
        if manager is None:
            manager = EgressProxyManager(client)
        logger.info("Reaper: tearing down orphaned egress triad %s (no cmd-executor)", token)
        try:
            await asyncio.to_thread(manager.teardown, token)
        except Exception:
            # Isolate per token: a teardown fault for one orphan (e.g. a daemon error from teardown's
            # own list calls) must not abort the sweep and starve the remaining orphans this tick.
            logger.exception("Reaper: failed to tear down orphaned egress triad %s", token)


async def _maybe_reap(
    client, redis, lock_manager, *, grace_seconds: int, max_stopped: int, activity, idle_seconds: int
) -> None:
    """Run one sweep, gated by a Redis leader lock so only one replica sweeps per tick.

    When ``redis`` is None (single-instance / no locking) the sweep runs inline.
    """
    now = datetime.now(UTC)
    sweep_kwargs = {
        "grace_seconds": grace_seconds,
        "max_stopped": max_stopped,
        "activity": activity,
        "idle_seconds": idle_seconds,
    }
    if redis is None:
        await _reap_once(client, lock_manager, now=now, **sweep_kwargs)
        return

    leader = redis.lock(_REAPER_LEADER_KEY, timeout=settings.REAPER_INTERVAL_SECONDS)
    if not await leader.acquire(blocking=False):
        logger.debug("Reaper: another replica holds the leader lock; skipping tick")
        return
    try:
        await _reap_once(client, lock_manager, now=now, **sweep_kwargs)
    finally:
        try:
            await leader.release()
        except Exception:
            logger.debug("Reaper: leader lock already released/expired")


async def _reaper_loop(
    client, redis, lock_manager, *, interval: int, grace_seconds: int, max_stopped: int, activity, idle_seconds: int
) -> None:
    """Sweep forever on a fixed cadence. A failed sweep is logged and the loop continues."""
    while True:
        try:
            await _maybe_reap(
                client,
                redis,
                lock_manager,
                grace_seconds=grace_seconds,
                max_stopped=max_stopped,
                activity=activity,
                idle_seconds=idle_seconds,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Reaper: sweep failed")
        await asyncio.sleep(interval)


def start_reaper(app) -> asyncio.Task | None:
    """Schedule the reaper loop as a background task, or None when disabled.

    Reads ``app.state.redis``, ``app.state.session_lock_manager`` and ``app.state.session_activity``
    set up in ``lifespan``.
    """
    if not settings.REAPER_ENABLED:
        logger.info("Reaper disabled (DAIV_SANDBOX_REAPER_ENABLED=false)")
        return None

    activity = app.state.session_activity
    if settings.RUNNING_SESSION_MAX_IDLE_SECONDS > 0 and not activity.enabled:
        # Otherwise the one leak with no other backstop is reintroduced with no runtime signal at all.
        logger.warning(
            "Reaper: idle-session reaping is configured (%ss) but disabled because DAIV_SANDBOX_REDIS_URL "
            "is not set; running sessions whose DELETE never arrives will leak indefinitely",
            settings.RUNNING_SESSION_MAX_IDLE_SECONDS,
        )

    client = SandboxDockerSession._get_shared_client()
    return asyncio.create_task(
        _reaper_loop(
            client,
            app.state.redis,
            app.state.session_lock_manager,
            interval=settings.REAPER_INTERVAL_SECONDS,
            grace_seconds=settings.SESSION_GRACE_SECONDS,
            max_stopped=settings.MAX_STOPPED_SESSIONS,
            activity=activity,
            idle_seconds=settings.RUNNING_SESSION_MAX_IDLE_SECONDS,
        )
    )
