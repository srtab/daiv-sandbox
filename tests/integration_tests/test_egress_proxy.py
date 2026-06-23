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
from daiv_sandbox.sessions import _build_single_file_tar_stream


@pytest.fixture
def docker_client():
    return docker.from_env()


def _image_present(client, ref) -> bool:
    try:
        client.images.get(ref)
        return True
    except docker.errors.ImageNotFound:
        return False


def _self_signed_ca() -> tuple[bytes, bytes]:
    """Return (combined_key_cert_pem, cert_pem).

    ``combined_key_cert_pem`` is the format mitmproxy loads from its confdir. ``cert_pem`` (the public
    cert alone) is installed into the sandbox so a client can trust the MITM'd leaf certificates the
    proxy mints from this CA — required whenever a flow is intercepted rather than tunnelled.
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
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    return key_pem + cert_pem, cert_pem


def _put_file(container, path: str, content: bytes) -> None:
    """Drop a single file into *container* at *path* via the archive API (no shell needed)."""
    parent, _, name = path.rpartition("/")
    with _build_single_file_tar_stream(name, content, mode=0o644) as tar:
        assert container.put_archive(parent or "/", tar)


def test_triad_blocks_unlisted_and_allows_listed(docker_client):
    if not _image_present(docker_client, settings.EGRESS_PROXY_IMAGE):
        pytest.skip("egress proxy image not built; run `make build-egress-proxy`")

    token = uuid.uuid4().hex[:12]
    mgr = EgressProxyManager(docker_client)
    sandbox = None
    try:
        combined, _ = _self_signed_ca()
        net = mgr.create_network(token)
        mgr.start_proxy(token, net, ca_pem=combined)
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

        # The deny must be the addon's 403 on the CONNECT, not a generic connection failure (an unready
        # or crashed proxy refuses every CONNECT with "000"). -sS surfaces curl's error message, which
        # quotes the proxy's 403 ("Received HTTP code 403 from proxy after CONNECT").
        blocked = sandbox.exec_run(
            ["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "10", "https://www.iana.org"],
            environment=env,
        )
        assert allowed.output.decode().strip().startswith(("2", "3"))  # reached upstream
        assert "403" in blocked.output.decode()  # specifically the addon's 403, not a bare "000"
    finally:
        if sandbox is not None:
            sandbox.remove(force=True)
        mgr.teardown(token)


def test_triad_mitm_injects_credential_and_enforces_method(docker_client):
    """MITM path against real mitmproxy: with intercept:"all" and the CA trusted, the request() hook
    actually runs — it enforces the per-host method limit (POST -> 403 post-TLS) and reaches its
    credential-injection branch. The reachability-only test above never exercises request() (everything
    is passed through), so this is the only real-container check of post-MITM method enforcement and the
    injection code path. (The injection check asserts the addon's "injected" log, i.e. the branch ran;
    it does not echo the header back, so it is not a full end-to-end proof the upstream received it.)"""
    if not _image_present(docker_client, settings.EGRESS_PROXY_IMAGE):
        pytest.skip("egress proxy image not built; run `make build-egress-proxy`")

    token = uuid.uuid4().hex[:12]
    mgr = EgressProxyManager(docker_client)
    sandbox = None
    try:
        combined, cert_pem = _self_signed_ca()
        net = mgr.create_network(token)
        mgr.start_proxy(token, net, ca_pem=combined)
        mgr.provision(
            token,
            json.dumps({
                "policy": {
                    "default": "deny",
                    "intercept": "all",  # example.com is MITM'd, so request() runs (method limit + inject)
                    "rules": [{"host": "example.com", "methods": ["GET"], "inject": "tok"}],
                },
                "secrets": {"tok": {"header": "Authorization", "value": "Bearer s3cr3t-test"}},
            }).encode(),
        )
        ip = mgr.proxy_internal_ip(token)
        env = exec_proxy_env(ip, settings.EGRESS_PROXY_PORT)

        ca_in_sandbox = "/tmp/egress-ca.crt"  # noqa: S108 - path inside an ephemeral test container, not the host
        sandbox = docker_client.containers.run("curlimages/curl:latest", command="sleep 120", detach=True, network=net)
        _put_file(sandbox, ca_in_sandbox, cert_pem)  # trust the MITM leaf certs minted from our CA

        def _curl(extra):
            return sandbox.exec_run(
                [
                    "curl",
                    "-sS",
                    "-o",
                    "/dev/null",
                    "-w",
                    "%{http_code}",
                    "--max-time",
                    "10",
                    "--cacert",
                    ca_in_sandbox,
                    *extra,
                ],
                environment=env,
            )

        # Poll the allowed GET until the proxy is listening AND the MITM leaf verifies against our CA.
        allowed = None
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            allowed = _curl(["https://example.com"])
            if allowed.output.decode().strip().startswith(("2", "3")):
                break
            time.sleep(0.25)
        assert allowed.output.decode().strip().startswith(("2", "3"))  # GET allowed, MITM'd, reached upstream

        # POST is enforced POST-MITM by request(): the addon's 403 arrives as a normal HTTP response over
        # the intercepted (trusted) TLS, so curl reports http_code 403 — not a CONNECT tunnel failure.
        post = _curl(["-X", "POST", "https://example.com"])
        assert post.output.decode().strip() == "403"

        # Unlisted host is still denied at CONNECT with the addon's 403.
        denied = _curl(["https://www.iana.org"])
        assert "403" in denied.output.decode()

        # The injection branch ran for the MITM'd GET: the addon logs the header it injected. (Proxy logs
        # only; we don't control an upstream that echoes headers back, so this proves the code path, not
        # upstream receipt — the post-MITM 403 above is the stronger proof that request() truly runs.)
        logs = mgr._proxy(token).logs().decode()
        assert "injected Authorization for example.com" in logs
    finally:
        if sandbox is not None:
            sandbox.remove(force=True)
        mgr.teardown(token)
