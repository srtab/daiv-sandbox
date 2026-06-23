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


async def _remove_guarded(container, lock_manager) -> bool:
    """Force-remove *container* while holding its per-session lock.

    Re-reads the container state under the lock before removing it: a request (e.g. a GET/run
    restart-on-access) may have warmed the container between the sweep's listing and now, which
    makes the list-time ``FinishedAt`` decision stale (a TOCTOU). The per-session lock serializes
    against in-flight requests, but it does not by itself prevent removing a container that was
    restarted just before the lock was acquired — so we re-check ``status`` and skip a session that
    is running again.

    Returns True if the container was removed (or had already vanished), False if the session was
    busy or is back in use (skip and let a later sweep retry).
    """
    try:
        async with lock_manager.acquire(container.id):
            await asyncio.to_thread(container.reload)
            if getattr(container, "status", None) == "running":
                logger.info("Reaper: container %s is running again; skipping removal", container.id)
                return False
            await asyncio.to_thread(container.remove, force=True)
            token = egress_token(container)
            if token:
                manager = EgressProxyManager(SandboxDockerSession._get_shared_client())
                await asyncio.to_thread(manager.teardown, token)
    except SessionBusyError:
        logger.info("Reaper: session %s busy; skipping this tick", container.id)
        return False
    except NotFound:
        return True
    except Exception:
        logger.exception("Reaper: failed to remove container %s", container.id)
        return False
    else:
        logger.info("Reaper: removed stopped container %s", container.id)
        return True


async def _reap_once(client, lock_manager, *, now, grace_seconds: int, max_stopped: int) -> None:
    """One sweep: remove stopped containers older than the grace window, then LRU-evict any beyond
    the count cap (oldest ``FinishedAt`` first). Containers with no parseable ``FinishedAt`` are
    kept and treated as newest for cap ordering."""
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

    if settings.egress_enabled:
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


async def _maybe_reap(client, redis, lock_manager, *, grace_seconds: int, max_stopped: int) -> None:
    """Run one sweep, gated by a Redis leader lock so only one replica sweeps per tick.

    When ``redis`` is None (single-instance / no locking) the sweep runs inline.
    """
    now = datetime.now(UTC)
    if redis is None:
        await _reap_once(client, lock_manager, now=now, grace_seconds=grace_seconds, max_stopped=max_stopped)
        return

    leader = redis.lock(_REAPER_LEADER_KEY, timeout=settings.REAPER_INTERVAL_SECONDS)
    if not await leader.acquire(blocking=False):
        logger.debug("Reaper: another replica holds the leader lock; skipping tick")
        return
    try:
        await _reap_once(client, lock_manager, now=now, grace_seconds=grace_seconds, max_stopped=max_stopped)
    finally:
        try:
            await leader.release()
        except Exception:
            logger.debug("Reaper: leader lock already released/expired")


async def _reaper_loop(client, redis, lock_manager, *, interval: int, grace_seconds: int, max_stopped: int) -> None:
    """Sweep forever on a fixed cadence. A failed sweep is logged and the loop continues."""
    while True:
        try:
            await _maybe_reap(client, redis, lock_manager, grace_seconds=grace_seconds, max_stopped=max_stopped)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Reaper: sweep failed")
        await asyncio.sleep(interval)


def start_reaper(app) -> asyncio.Task | None:
    """Schedule the reaper loop as a background task, or None when disabled.

    Reads ``app.state.redis`` and ``app.state.session_lock_manager`` set up in ``lifespan``.
    """
    if not settings.REAPER_ENABLED:
        logger.info("Reaper disabled (DAIV_SANDBOX_REAPER_ENABLED=false)")
        return None

    client = SandboxDockerSession._get_shared_client()
    return asyncio.create_task(
        _reaper_loop(
            client,
            app.state.redis,
            app.state.session_lock_manager,
            interval=settings.REAPER_INTERVAL_SECONDS,
            grace_seconds=settings.SESSION_GRACE_SECONDS,
            max_stopped=settings.MAX_STOPPED_SESSIONS,
        )
    )
