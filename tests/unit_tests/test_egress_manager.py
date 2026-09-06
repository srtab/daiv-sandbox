from unittest.mock import MagicMock, Mock

import pytest
from docker.errors import APIError, ImageNotFound, NotFound

from daiv_sandbox.egress.manager import EgressProxyManager, exec_proxy_env
from daiv_sandbox.sessions import EGRESS_SESSION_LABEL, TYPE_EGRESS_NETWORK, TYPE_EGRESS_PROXY, SessionUnavailableError


def test_create_network_is_internal_and_labeled():
    client = MagicMock()
    client.networks.create.return_value = MagicMock(name="net")
    EgressProxyManager(client).create_network("tok123")
    kwargs = client.networks.create.call_args.kwargs
    assert kwargs["internal"] is True
    assert kwargs["labels"][EGRESS_SESSION_LABEL] == "tok123"
    assert kwargs["labels"]["daiv.sandbox.type"] == TYPE_EGRESS_NETWORK


def test_start_proxy_creates_dualhomed_labeled_container(monkeypatch):
    from daiv_sandbox.config import settings

    monkeypatch.setattr(settings, "EGRESS_PROXY_IMAGE", "img:test")
    monkeypatch.setattr(settings, "EGRESS_PROXY_NETWORK", "egress-net")
    client = MagicMock()
    proxy = client.containers.create.return_value
    mgr = EgressProxyManager(client)

    mgr.start_proxy("tok123", "daiv-egress-tok123", ca_pem=b"PEM")

    cargs = client.containers.create.call_args
    assert cargs.kwargs["image"] == "img:test"
    assert cargs.kwargs["network"] == "daiv-egress-tok123"  # internal NIC at create
    assert cargs.kwargs["labels"][EGRESS_SESSION_LABEL] == "tok123"
    assert cargs.kwargs["labels"]["daiv.sandbox.type"] == TYPE_EGRESS_PROXY
    # second NIC (egress) connected before start; CA injected; then started
    client.networks.get.assert_called_with("egress-net")
    client.networks.get.return_value.connect.assert_called_once_with(proxy)
    assert proxy.put_archive.called  # CA shipped into confdir
    proxy.start.assert_called_once()
    # The warm path must not reach the registry, nor create the container twice.
    client.images.pull.assert_not_called()
    client.containers.create.assert_called_once()


def test_start_proxy_pulls_and_retries_when_sidecar_image_missing(monkeypatch):
    """A host that reclaimed the sidecar image (`docker image prune -a`, or one that never pulled it)
    must self-heal instead of failing every new egress session."""
    from daiv_sandbox.config import settings

    monkeypatch.setattr(settings, "EGRESS_PROXY_IMAGE", "img:test")
    monkeypatch.setattr(settings, "EGRESS_PROXY_NETWORK", "egress-net")
    client = MagicMock()
    proxy = MagicMock()
    client.containers.create.side_effect = [ImageNotFound("No such image: img:test"), proxy]
    mgr = EgressProxyManager(client)

    assert mgr.start_proxy("tok123", "daiv-egress-tok123", ca_pem=b"PEM") is proxy

    client.images.pull.assert_called_once_with("img:test")
    assert client.containers.create.call_count == 2  # the retry after the pull is what heals it
    proxy.start.assert_called_once()


def test_start_proxy_retries_the_create_exactly_once(monkeypatch):
    """An image still absent after a successful pull must give up rather than loop: an unbounded
    pull-and-retry would hang the request while hammering the registry."""
    from daiv_sandbox.config import settings

    monkeypatch.setattr(settings, "EGRESS_PROXY_IMAGE", "img:test")
    monkeypatch.setattr(settings, "EGRESS_PROXY_NETWORK", "egress-net")
    client = MagicMock()
    client.containers.create.side_effect = ImageNotFound("No such image: img:test")
    mgr = EgressProxyManager(client)

    with pytest.raises(ImageNotFound):
        mgr.start_proxy("tok123", "daiv-egress-tok123", ca_pem=b"PEM")

    assert client.containers.create.call_count == 2
    client.images.pull.assert_called_once_with("img:test")


def test_start_proxy_reports_registry_fault_instead_of_image_not_found(monkeypatch):
    """A registry fault (ENOSPC, rate limit) must surface naming the image, not as the misleading
    ImageNotFound that docker-py's images.pull re-raises when the pull itself failed."""
    from daiv_sandbox.config import settings

    monkeypatch.setattr(settings, "EGRESS_PROXY_IMAGE", "img:test")
    monkeypatch.setattr(settings, "EGRESS_PROXY_NETWORK", "egress-net")
    client = MagicMock()
    client.containers.create.side_effect = ImageNotFound("No such image: img:test")
    client.images.pull.side_effect = ImageNotFound("No such image: img:test")
    mgr = EgressProxyManager(client)

    with pytest.raises(RuntimeError, match="cannot pull sidecar image img:test"):
        mgr.start_proxy("tok123", "daiv-egress-tok123", ca_pem=b"PEM")

    assert client.containers.create.call_count == 1  # no retry once the pull is known to have failed


def test_start_proxy_propagates_daemon_fault_without_pulling(monkeypatch):
    """A create failure that is not a missing image must propagate untouched — widening the catch from
    ImageNotFound to APIError would turn a daemon fault into a pointless registry pull."""
    from daiv_sandbox.config import settings

    monkeypatch.setattr(settings, "EGRESS_PROXY_IMAGE", "img:test")
    monkeypatch.setattr(settings, "EGRESS_PROXY_NETWORK", "egress-net")
    client = MagicMock()
    client.containers.create.side_effect = APIError("500 Server Error: daemon unreachable")
    mgr = EgressProxyManager(client)

    with pytest.raises(APIError):
        mgr.start_proxy("tok123", "daiv-egress-tok123", ca_pem=b"PEM")

    client.images.pull.assert_not_called()


def test_start_proxy_sets_onfailure_restart_policy(monkeypatch):
    """The sidecar must carry an on-failure restart policy so the daemon auto-recovers it after a
    crash/OOM (non-zero exit) — otherwise a dead proxy poisons a still-warm session's egress until a
    request happens to warm-restart it. It must NOT be `always`/`unless-stopped`: a proxy that OOMs on
    every boot would thrash forever on a memory-starved host; on-failure is bounded (MaximumRetryCount)
    and leaves a clean exit-0 shutdown alone."""
    from daiv_sandbox.config import settings

    monkeypatch.setattr(settings, "EGRESS_PROXY_IMAGE", "img:test")
    monkeypatch.setattr(settings, "EGRESS_PROXY_NETWORK", "egress-net")
    client = MagicMock()
    mgr = EgressProxyManager(client)

    mgr.start_proxy("tok123", "daiv-egress-tok123", ca_pem=b"PEM")

    policy = client.containers.create.call_args.kwargs["restart_policy"]
    assert policy["Name"] == "on-failure"
    assert policy.get("MaximumRetryCount", 0) > 0  # bounded, so it can't thrash forever


def test_exec_proxy_env_points_clients_at_proxy_and_ca():
    env = exec_proxy_env("10.0.0.2", 8080)
    assert env["HTTPS_PROXY"] == "http://10.0.0.2:8080"
    assert env["http_proxy"] == "http://10.0.0.2:8080"
    assert env["REQUESTS_CA_BUNDLE"].endswith("ca-certificates.crt")
    assert env["NODE_EXTRA_CA_CERTS"].endswith("ca-certificates.crt")


def test_start_proxy_raises_when_ca_put_archive_fails(monkeypatch):
    """If the CA copy into the sidecar confdir is rejected, start_proxy must fail closed (raise) and
    NOT boot the proxy — otherwise mitmdump starts with a self-generated CA the sandbox won't trust,
    silently diverging from the configured posture."""
    from daiv_sandbox.config import settings

    monkeypatch.setattr(settings, "EGRESS_PROXY_IMAGE", "img:test")
    monkeypatch.setattr(settings, "EGRESS_PROXY_NETWORK", "egress-net")
    client = MagicMock()
    proxy = client.containers.create.return_value
    proxy.put_archive.return_value = False  # daemon rejected the CA copy
    mgr = EgressProxyManager(client)

    with pytest.raises(RuntimeError, match="CA"):
        mgr.start_proxy("tok123", "daiv-egress-tok123", ca_pem=b"PEM")
    proxy.start.assert_not_called()


def test_start_proxy_removes_created_container_on_failure(monkeypatch):
    """If a step after containers.create fails (e.g. wiring the second NIC), start_proxy must remove its
    own container before propagating — so a caller that forgot the teardown wrapper doesn't leak it."""
    from daiv_sandbox.config import settings

    monkeypatch.setattr(settings, "EGRESS_PROXY_IMAGE", "img:test")
    monkeypatch.setattr(settings, "EGRESS_PROXY_NETWORK", "egress-net")
    client = MagicMock()
    proxy = client.containers.create.return_value
    client.networks.get.return_value.connect.side_effect = APIError("connect failed")
    mgr = EgressProxyManager(client)

    with pytest.raises(APIError):
        mgr.start_proxy("tok123", "daiv-egress-tok123", ca_pem=b"PEM")
    proxy.remove.assert_called_once_with(force=True, v=True)
    proxy.start.assert_not_called()


def test_teardown_removes_proxy_and_network():
    client = MagicMock()
    proxy, net = MagicMock(), MagicMock()
    client.containers.list.return_value = [proxy]
    client.networks.list.return_value = [net]
    EgressProxyManager(client).teardown("tok123")
    proxy.remove.assert_called_once_with(force=True, v=True)
    net.remove.assert_called_once()


def test_teardown_suppresses_notfound_on_proxy_remove():
    """A proxy already gone (NotFound) is fine and must not stop network cleanup."""
    client = MagicMock()
    proxy, net = MagicMock(), MagicMock()
    proxy.remove.side_effect = NotFound("already gone")
    client.containers.list.return_value = [proxy]
    client.networks.list.return_value = [net]
    EgressProxyManager(client).teardown("tok123")  # must not raise
    net.remove.assert_called_once()


def test_teardown_logs_and_continues_when_proxy_remove_errors():
    """A non-NotFound proxy-remove failure (e.g. daemon busy) must be logged and swallowed, NOT
    propagated — otherwise it masks the caller's original error and skips network cleanup, leaking
    the internal network."""
    client = MagicMock()
    proxy, net = MagicMock(), MagicMock()
    proxy.remove.side_effect = APIError("daemon busy")
    client.containers.list.return_value = [proxy]
    client.networks.list.return_value = [net]
    EgressProxyManager(client).teardown("tok123")  # must not raise
    net.remove.assert_called_once()  # network still cleaned up despite the proxy failure


def test_suspend_stops_proxy_and_drops_network_but_keeps_both_containers():
    """A non-force close stops the sidecar to free its memory AND drops the internal network to return
    its subnet to Docker's host-wide address pool — the resource that actually runs out (only ~29 are
    usable, far below MAX_STOPPED_SESSIONS). Neither container may be removed: both are preserved so a
    resumed session warm-restarts the proxy from its provisioned confdir. Removal stays in teardown."""
    from daiv_sandbox.config import settings

    client = MagicMock()
    proxy, executor, net = MagicMock(), MagicMock(), MagicMock()
    client.containers.list.side_effect = [[proxy], [executor]]
    client.networks.list.return_value = [net]

    EgressProxyManager(client).suspend("tok123")

    # The explicit STOP_TIMEOUT is deliberate (fast memory reclaim); assert it, don't let a silent
    # revert to Docker's 10s default pass — mirrors test_sessions.py's stop_container assertion.
    proxy.stop.assert_called_once_with(timeout=settings.STOP_TIMEOUT_SECONDS)
    net.remove.assert_called_once()
    proxy.remove.assert_not_called()
    executor.remove.assert_not_called()


def test_suspend_disconnects_members_before_removing_the_network():
    """Both members must be disconnected BEFORE the network is removed. Docker resolves an endpoint by
    network ID, so a container left attached to a removed network can never start again — it fails with
    "network <id> not found" even once a same-name network exists. Ordering is the whole point here, so
    assert the sequence, not just the calls."""
    client = MagicMock()
    proxy, executor, net = MagicMock(), MagicMock(), MagicMock()
    client.containers.list.side_effect = [[proxy], [executor]]
    client.networks.list.return_value = [net]
    calls = []
    net.disconnect.side_effect = lambda member, **kw: calls.append(("disconnect", member))
    net.remove.side_effect = lambda: calls.append(("remove", None))

    EgressProxyManager(client).suspend("tok123")

    assert calls == [("disconnect", proxy), ("disconnect", executor), ("remove", None)]


def test_suspend_suppresses_notfound():
    """A proxy already gone (NotFound) is success and must not raise."""
    client = MagicMock()
    proxy = MagicMock()
    proxy.stop.side_effect = NotFound("already gone")
    client.containers.list.side_effect = [[proxy], []]
    client.networks.list.return_value = []
    EgressProxyManager(client).suspend("tok123")  # must not raise
    proxy.stop.assert_called_once()  # the swallow happened at the intended call site, not by skipping it


def test_suspend_logs_and_swallows_other_errors():
    """A non-NotFound stop failure (e.g. daemon busy) must be logged and swallowed, not propagated,
    so a stuck sidecar never fails the DELETE — teardown (force close / reaper) is the backstop."""
    client = MagicMock()
    proxy = MagicMock()
    proxy.stop.side_effect = APIError("daemon busy")
    client.containers.list.side_effect = [[proxy], []]
    client.networks.list.return_value = []
    EgressProxyManager(client).suspend("tok123")  # must not raise
    proxy.stop.assert_called_once()  # the swallow happened at the intended call site, not by skipping it


def test_suspend_still_removes_the_network_when_a_disconnect_fails():
    """A stuck disconnect must not strand the subnet: the network removal is the whole point of the
    suspend, so one bad member is logged and skipped rather than aborting the cleanup."""
    client = MagicMock()
    proxy, net = MagicMock(), MagicMock()
    client.containers.list.side_effect = [[proxy], []]
    client.networks.list.return_value = [net]
    net.disconnect.side_effect = APIError("daemon busy")

    EgressProxyManager(client).suspend("tok123")  # must not raise

    net.remove.assert_called_once()


def _network(name: str = "daiv-egress-tok123", net_id: str = "net-new") -> MagicMock:
    net = MagicMock()
    net.name = name
    net.id = net_id
    return net


def _member(network_name: str | None = None, net_id: str = "net-new") -> MagicMock:
    member = MagicMock()
    networks = {network_name: {"NetworkID": net_id}} if network_name else {}
    member.attrs = {"NetworkSettings": {"Networks": networks}}
    return member


def test_resume_recreates_the_network_when_it_is_gone():
    """After a suspend the labelled network no longer exists, so resume must rebuild it under the same
    name before anything tries to start."""
    client = MagicMock()
    client.networks.list.return_value = []
    client.containers.list.side_effect = [[], []]
    client.networks.get.return_value = _network()

    assert EgressProxyManager(client).resume("tok123") == "daiv-egress-tok123"

    assert client.networks.create.call_args.kwargs["name"] == "daiv-egress-tok123"


def test_resume_reattaches_both_members_before_either_starts():
    """Both the sidecar and the sandbox must be reconnected by resume. Reattaching only the proxy would
    leave the sandbox wedged on its next start with "network <id> not found"."""
    client = MagicMock()
    net = _network()
    client.networks.list.return_value = [net]
    proxy, executor = _member(), _member()
    client.containers.list.side_effect = [[proxy], [executor]]

    EgressProxyManager(client).resume("tok123")

    assert [c.args[0] for c in net.connect.call_args_list] == [proxy, executor]


def test_resume_is_a_noop_for_members_already_on_the_live_network():
    """resume runs on the request hot path, so an already-attached member must cost nothing — no
    reconnect churn, and no spurious disconnect that would drop a live session's egress."""
    client = MagicMock()
    net = _network(net_id="net-live")
    client.networks.list.return_value = [net]
    attached = _member("daiv-egress-tok123", net_id="net-live")
    client.containers.list.side_effect = [[attached], []]

    EgressProxyManager(client).resume("tok123")

    net.connect.assert_not_called()
    net.disconnect.assert_not_called()


def test_resume_clears_a_stale_endpoint_before_reconnecting():
    """A member left pointing at a PREVIOUS network generation (same name, dead ID — a partially failed
    suspend) cannot simply be connected: Docker refuses, and the container stays unstartable. Force-
    disconnect clears the dead endpoint even though the live network's ID differs, then connect."""
    client = MagicMock()
    net = _network(net_id="net-new")
    client.networks.list.return_value = [net]
    stale = _member("daiv-egress-tok123", net_id="net-old-and-removed")
    client.containers.list.side_effect = [[stale], []]

    EgressProxyManager(client).resume("tok123")

    net.disconnect.assert_called_once_with(stale, force=True)
    net.connect.assert_called_once_with(stale)


def _proxy_with_ip(ip: str, network_name: str = "daiv-egress-tok123") -> Mock:
    proxy = Mock()
    proxy.attrs = {"NetworkSettings": {"Networks": {network_name: {"IPAddress": ip}}}}
    return proxy


def test_proxy_internal_ip_returns_ip_of_running_proxy_without_restart(monkeypatch):
    """The common path: a running proxy with an IP is resolved directly, never restarted."""
    mgr = EgressProxyManager(Mock())
    proxy = _proxy_with_ip("10.1.2.3")
    proxy.status = "running"
    monkeypatch.setattr(mgr, "_proxy", lambda token: proxy)
    monkeypatch.setattr(mgr, "resume", lambda token: None)  # network rebuild covered by its own tests

    assert mgr.proxy_internal_ip("tok123") == "10.1.2.3"
    proxy.restart.assert_not_called()


def test_proxy_internal_ip_restarts_stopped_proxy_and_reresolves(monkeypatch):
    """A proxy sidecar can be OOM-killed (or lost to a daemon restart) while its session's sandbox
    stays warm, and the restart policy may not have fired yet. proxy_internal_ip must warm-restart the
    stopped proxy — same restart-on-access idea as the sandbox — and return the fresh IP, so the
    session self-heals instead of silently losing egress forever.

    The fresh IP is modeled to appear only on the reload() *after* restart() — as with real docker-py,
    where restart() does not refresh .attrs — so this test guards the reload-after-restart call: drop
    it and the method reads back the stale (empty) IP and this assertion fails."""
    mgr = EgressProxyManager(Mock())
    proxy = _proxy_with_ip("")  # stopped: Docker releases the endpoint IP
    proxy.status = "exited"

    def _reload():
        # docker-py refreshes .attrs on reload(), not on restart(): the fresh endpoint IP only appears
        # on the reload() *after* restart(). Keying the side effect on restart.called guards that
        # post-restart reload — drop it and attrs stay empty and the assertion fails with "has no IP".
        if proxy.restart.called:
            proxy.attrs = _proxy_with_ip("10.9.9.9").attrs

    proxy.reload.side_effect = _reload
    monkeypatch.setattr(mgr, "_proxy", lambda token: proxy)
    monkeypatch.setattr(mgr, "resume", lambda token: None)  # network rebuild covered by its own tests

    assert mgr.proxy_internal_ip("tok123") == "10.9.9.9"
    proxy.restart.assert_called_once()
    assert proxy.reload.call_count == 2  # once before the status check, once after restart


def test_proxy_internal_ip_raises_session_unavailable_when_restart_fails(monkeypatch):
    """If the dead proxy cannot be restarted (daemon/runtime fault), surface a retryable
    SessionUnavailableError (503) rather than the opaque "has no IP" RuntimeError."""
    mgr = EgressProxyManager(Mock())
    proxy = _proxy_with_ip("")
    proxy.status = "exited"
    proxy.restart.side_effect = APIError("daemon refused restart")
    monkeypatch.setattr(mgr, "_proxy", lambda token: proxy)
    monkeypatch.setattr(mgr, "resume", lambda token: None)  # network rebuild covered by its own tests

    with pytest.raises(SessionUnavailableError):
        mgr.proxy_internal_ip("tok123")


def test_proxy_internal_ip_raises_when_running_proxy_has_no_ip(monkeypatch):
    """A genuinely anomalous state — proxy running yet holding no IP on its internal network — still
    raises the explicit RuntimeError (it is not a stopped-proxy case, so no restart is attempted)."""
    mgr = EgressProxyManager(Mock())
    proxy = _proxy_with_ip("")
    proxy.status = "running"
    monkeypatch.setattr(mgr, "_proxy", lambda token: proxy)
    monkeypatch.setattr(mgr, "resume", lambda token: None)  # network rebuild covered by its own tests

    with pytest.raises(RuntimeError, match="has no IP"):
        mgr.proxy_internal_ip("tok123")
    proxy.restart.assert_not_called()


def test_ensure_proxy_running_restarts_stopped_proxy(monkeypatch):
    """A warm-stopped session's sidecar (stopped by a non-force close, or an OOM / daemon restart) is
    warm-restarted on access so a caller that needs it running — provision on an egress refresh —
    self-heals instead of failing. Same restart-on-access as proxy_internal_ip."""
    mgr = EgressProxyManager(Mock())
    proxy = Mock()
    proxy.status = "exited"
    monkeypatch.setattr(mgr, "_proxy", lambda token: proxy)
    monkeypatch.setattr(mgr, "resume", lambda token: None)  # network rebuild covered by its own tests

    assert mgr.ensure_proxy_running("tok123") is proxy
    proxy.restart.assert_called_once()
    assert proxy.reload.call_count == 2  # once before the status check, once after restart


def test_ensure_proxy_running_rebuilds_the_network_before_restarting(monkeypatch):
    """The sidecar of a suspended session has no network to come back to, so resume must run BEFORE the
    restart — reversed, the restart fails with "network <id> not found" and the session is stuck."""
    mgr = EgressProxyManager(Mock())
    proxy = Mock()
    proxy.status = "exited"
    calls = []
    monkeypatch.setattr(mgr, "_proxy", lambda token: proxy)
    monkeypatch.setattr(mgr, "resume", lambda token: calls.append("resume"))
    proxy.restart.side_effect = lambda: calls.append("restart")

    mgr.ensure_proxy_running("tok123")

    assert calls == ["resume", "restart"]


def test_ensure_proxy_running_skips_the_rebuild_for_a_running_proxy(monkeypatch):
    """resume must stay off the hot path: a running proxy proves its network exists, so the per-request
    path must not pay for two extra Docker list calls."""
    mgr = EgressProxyManager(Mock())
    proxy = Mock()
    proxy.status = "running"
    resumed = []
    monkeypatch.setattr(mgr, "_proxy", lambda token: proxy)
    monkeypatch.setattr(mgr, "resume", lambda token: resumed.append(token))

    mgr.ensure_proxy_running("tok123")

    assert resumed == []


def test_ensure_proxy_running_leaves_running_proxy_untouched(monkeypatch):
    """A proxy already running is returned without a needless restart."""
    mgr = EgressProxyManager(Mock())
    proxy = Mock()
    proxy.status = "running"
    monkeypatch.setattr(mgr, "_proxy", lambda token: proxy)
    monkeypatch.setattr(mgr, "resume", lambda token: None)  # network rebuild covered by its own tests

    assert mgr.ensure_proxy_running("tok123") is proxy
    proxy.restart.assert_not_called()


def test_ensure_proxy_running_raises_session_unavailable_when_restart_fails(monkeypatch):
    """A restart fault surfaces as a retryable SessionUnavailableError (503), not an opaque 500."""
    mgr = EgressProxyManager(Mock())
    proxy = Mock()
    proxy.status = "exited"
    proxy.restart.side_effect = APIError("daemon refused restart")
    monkeypatch.setattr(mgr, "_proxy", lambda token: proxy)
    monkeypatch.setattr(mgr, "resume", lambda token: None)  # network rebuild covered by its own tests

    with pytest.raises(SessionUnavailableError):
        mgr.ensure_proxy_running("tok123")


def _provision_ready_mgr(monkeypatch, *, status: str = "running"):
    """An EgressProxyManager whose _proxy() returns a proxy wired for a successful provision.

    Tests override a single attribute (put_archive/exec_run) to exercise one specific failure path, or
    pass ``status`` to start from a proxy provision has to warm-restart first.
    """
    mgr = EgressProxyManager(Mock())
    proxy = Mock()
    proxy.status = status
    proxy.put_archive.return_value = True
    proxy.exec_run.return_value = Mock(exit_code=0, output=b"")
    monkeypatch.setattr(mgr, "_proxy", lambda token: proxy)
    monkeypatch.setattr(mgr, "resume", lambda token: None)  # network rebuild covered by its own tests
    return mgr, proxy


def test_provision_stages_to_temp_then_renames_atomically(monkeypatch):
    """provision must put_archive a temp file, then rename it over config.json (atomic swap)."""
    mgr, proxy = _provision_ready_mgr(monkeypatch)

    mgr.provision("tok123", b'{"policy": {"default": "allow"}, "secrets": {}}')

    proxy.put_archive.assert_called_once()
    assert proxy.put_archive.call_args.args[0] == "/run/egress"
    proxy.exec_run.assert_called_once_with(
        ["mv", "-f", "/run/egress/config.json.tmp", "/run/egress/config.json"], user="root"
    )
    proxy.reload.assert_called_once()


def test_provision_raises_when_rename_fails(monkeypatch):
    """A non-zero rename exit must fail loud so a half-applied config never goes unnoticed."""
    mgr, proxy = _provision_ready_mgr(monkeypatch)
    proxy.exec_run.return_value = Mock(exit_code=1, output=b"mv: cannot move")

    with pytest.raises(RuntimeError, match="failed to install config"):
        mgr.provision("tok123", b"{}")


def test_provision_warm_restarts_stopped_proxy_then_writes(monkeypatch):
    """A warm-stopped proxy (the normal warm-reuse state, since a non-force close stops it) is
    warm-restarted by provision and then written — provision no longer 503s on a merely-stopped proxy,
    so an egress refresh on a resumed session succeeds instead of forcing a session recreate."""
    mgr = EgressProxyManager(Mock())
    proxy = Mock()
    proxy.status = "exited"
    proxy.put_archive.return_value = True
    proxy.exec_run.return_value = Mock(exit_code=0, output=b"")
    monkeypatch.setattr(mgr, "_proxy", lambda token: proxy)
    monkeypatch.setattr(mgr, "resume", lambda token: None)  # network rebuild covered by its own tests

    mgr.provision("tok123", b"{}")

    proxy.restart.assert_called_once()
    proxy.put_archive.assert_called_once()
    proxy.exec_run.assert_called_once()


def test_provision_raises_session_unavailable_when_restart_fails(monkeypatch):
    """If a stopped proxy cannot be warm-restarted (daemon/runtime fault), provision surfaces a
    retryable 503 and writes nothing — no torn config."""
    mgr = EgressProxyManager(Mock())
    proxy = Mock()
    proxy.status = "exited"
    proxy.restart.side_effect = APIError("daemon refused restart")
    monkeypatch.setattr(mgr, "_proxy", lambda token: proxy)
    monkeypatch.setattr(mgr, "resume", lambda token: None)  # network rebuild covered by its own tests

    with pytest.raises(SessionUnavailableError):
        mgr.provision("tok123", b"{}")

    proxy.put_archive.assert_not_called()
    proxy.exec_run.assert_not_called()


def test_provision_translates_apierror_during_mv_to_session_unavailable(monkeypatch):
    """A proxy that stops between the status check and the mv must surface as retryable 503, not 500."""
    mgr, proxy = _provision_ready_mgr(monkeypatch)
    proxy.exec_run.side_effect = APIError("Container abc is not running")

    with pytest.raises(SessionUnavailableError):
        mgr.provision("tok123", b"{}")

    proxy.put_archive.assert_called_once()  # staging happened; the race is on the rename


def test_provision_translates_notfound_proxy_to_session_unavailable(monkeypatch):
    """A reaped/removed proxy (NotFound, an APIError subclass) must surface as retryable 503."""
    mgr = EgressProxyManager(Mock())
    monkeypatch.setattr(mgr, "_proxy", Mock(side_effect=NotFound("no proxy")))

    with pytest.raises(SessionUnavailableError):
        mgr.provision("tok123", b"{}")


def test_provision_raises_when_put_archive_fails(monkeypatch):
    """A daemon-rejected staging copy (put_archive False) must fail loud, not silently skip the write."""
    mgr, proxy = _provision_ready_mgr(monkeypatch)
    proxy.put_archive.return_value = False

    with pytest.raises(RuntimeError, match="failed to stage config"):
        mgr.provision("tok123", b"{}")

    proxy.exec_run.assert_not_called()  # never attempt the rename if staging failed
