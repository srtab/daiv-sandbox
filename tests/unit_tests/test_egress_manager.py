from unittest.mock import MagicMock, Mock

import pytest
from docker.errors import APIError, NotFound

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
    proxy.remove.assert_called_once_with(force=True)
    proxy.start.assert_not_called()


def test_teardown_removes_proxy_and_network():
    client = MagicMock()
    proxy, net = MagicMock(), MagicMock()
    client.containers.list.return_value = [proxy]
    client.networks.list.return_value = [net]
    EgressProxyManager(client).teardown("tok123")
    proxy.remove.assert_called_once_with(force=True)
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


def test_provision_stages_to_temp_then_renames_atomically(monkeypatch):
    """provision must put_archive a temp file, then rename it over config.json (atomic swap)."""
    mgr = EgressProxyManager(Mock())
    proxy = Mock()
    proxy.status = "running"
    proxy.put_archive.return_value = True
    proxy.exec_run.return_value = Mock(exit_code=0, output=b"")
    monkeypatch.setattr(mgr, "_proxy", lambda token: proxy)

    mgr.provision("tok123", b'{"policy": {"default": "allow"}, "secrets": {}}')

    proxy.put_archive.assert_called_once()
    assert proxy.put_archive.call_args.args[0] == "/run/egress"
    proxy.exec_run.assert_called_once_with(
        ["mv", "-f", "/run/egress/config.json.tmp", "/run/egress/config.json"], user="root"
    )


def test_provision_raises_when_rename_fails(monkeypatch):
    """A non-zero rename exit must fail loud so a half-applied config never goes unnoticed."""
    mgr = EgressProxyManager(Mock())
    proxy = Mock()
    proxy.status = "running"
    proxy.put_archive.return_value = True
    proxy.exec_run.return_value = Mock(exit_code=1, output=b"mv: cannot move")
    monkeypatch.setattr(mgr, "_proxy", lambda token: proxy)

    with pytest.raises(RuntimeError, match="failed to install config"):
        mgr.provision("tok123", b"{}")


def test_provision_raises_session_unavailable_when_proxy_not_running(monkeypatch):
    """A stopped proxy (e.g. after a daemon restart) must fail as a retryable 503, not a torn write."""
    mgr = EgressProxyManager(Mock())
    proxy = Mock()
    proxy.status = "exited"
    monkeypatch.setattr(mgr, "_proxy", lambda token: proxy)

    with pytest.raises(SessionUnavailableError):
        mgr.provision("tok123", b"{}")

    proxy.put_archive.assert_not_called()
    proxy.exec_run.assert_not_called()
