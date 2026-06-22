"""Real-container triad test. Requires a Docker daemon AND the sidecar image built locally
(`make build-egress-proxy`). Skipped otherwise."""

import datetime
import json
import time
import uuid

import docker
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from daiv_sandbox.config import settings
from daiv_sandbox.egress.manager import EgressProxyManager, exec_proxy_env


@pytest.fixture
def docker_client():
    return docker.from_env()


def _image_present(client, ref) -> bool:
    try:
        client.images.get(ref)
        return True
    except docker.errors.ImageNotFound:
        return False


def _self_signed_ca_pem() -> bytes:
    """A valid combined key+cert PEM, the format mitmproxy loads from its confdir.

    We only assert allow/deny reachability (the allowed host is tunnelled in passthrough, the denied
    host is rejected at CONNECT), so the cert is never trusted by a client — but mitmproxy still needs
    a structurally valid CA to start, so a self-signed throwaway is enough.
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "daiv-egress-test")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509
        .CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
    )
    return key_pem + cert.public_bytes(serialization.Encoding.PEM)


def test_triad_blocks_unlisted_and_allows_listed(docker_client):
    if not _image_present(docker_client, settings.EGRESS_PROXY_IMAGE):
        pytest.skip("egress proxy image not built; run `make build-egress-proxy`")

    token = uuid.uuid4().hex[:12]
    mgr = EgressProxyManager(docker_client)
    sandbox = None
    try:
        net = mgr.create_network(token)
        mgr.start_proxy(token, net, ca_pem=_self_signed_ca_pem())
        # `methods: ["*"]` is what the addon's CONNECT-phase reachability gate matches against: it
        # evaluates the literal "CONNECT" method, so a method-restricted rule (e.g. ["GET","HEAD"])
        # would deny the TLS tunnel before any GET could be sent. "*" is the host-allow pattern the
        # addon was designed and unit-tested for (see tests/unit_tests/test_egress_addon.py).
        mgr.provision(
            token,
            json.dumps({
                "policy": {
                    "default": "deny",
                    "intercept": "credentialed",
                    "rules": [{"host": "example.com", "methods": ["*"]}],
                },
                "secrets": {},
            }).encode(),
        )
        ip = mgr.proxy_internal_ip(token)
        env = exec_proxy_env(ip, settings.EGRESS_PROXY_PORT)

        sandbox = docker_client.containers.run("curlimages/curl:latest", command="sleep 60", detach=True, network=net)

        def _curl(host: str):
            return sandbox.exec_run(
                ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "10", host], environment=env
            )

        # mitmproxy takes ~1s to start listening; poll the allowed host until the tunnel succeeds.
        # This also rules out a false-positive deny below: an unready proxy refuses *every* CONNECT
        # ("000"), so we must confirm the proxy is up before asserting iana.org is policy-blocked.
        allowed = None
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            allowed = _curl("https://example.com")
            if allowed.output.decode().strip().startswith(("2", "3")):
                break
            time.sleep(0.25)

        blocked = _curl("https://www.iana.org")
        assert allowed.output.decode().strip().startswith(("2", "3"))  # reached upstream
        assert blocked.exit_code != 0 or blocked.output.decode().strip() in ("000", "403")
    finally:
        if sandbox is not None:
            sandbox.remove(force=True)
        mgr.teardown(token)
