from unittest.mock import MagicMock

from daiv_sandbox.egress.manager import EgressProxyManager, exec_proxy_env
from daiv_sandbox.sessions import EGRESS_SESSION_LABEL, TYPE_EGRESS_NETWORK, TYPE_EGRESS_PROXY


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


def test_teardown_removes_proxy_and_network():
    client = MagicMock()
    proxy, net = MagicMock(), MagicMock()
    client.containers.list.return_value = [proxy]
    client.networks.list.return_value = [net]
    EgressProxyManager(client).teardown("tok123")
    proxy.remove.assert_called_once_with(force=True)
    net.remove.assert_called_once()
