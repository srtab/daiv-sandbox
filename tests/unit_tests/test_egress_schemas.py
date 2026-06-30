import pytest
from pydantic import ValidationError

from daiv_sandbox.schemas import EgressConfigRequest, EgressPolicy, EgressRule, EgressSecret


def test_methods_uppercased_and_default_star():
    rule = EgressRule(host="github.com")
    assert rule.methods == ["*"]
    assert EgressRule(host="x", methods=["get", "Head"]).methods == ["GET", "HEAD"]


def test_empty_methods_raises_validation_error():
    """An explicit empty methods list is a footgun: the host is reachable via CONNECT but every
    request is blocked. Reject it at validation time so the operator sees a clear error."""
    with pytest.raises(ValidationError, match="must not be empty"):
        EgressRule(host="x", methods=[])


@pytest.mark.parametrize("host", ["", "   ", "git hub.com", "github.com\n", "bad\x00host"])
def test_invalid_host_raises_validation_error(host):
    """The host glob is matched verbatim (lower-cased) in the sidecar; an empty glob or one carrying
    whitespace/control characters can never match a real host and signals a malformed rule — reject it
    at validation time rather than letting it silently no-op."""
    with pytest.raises(ValidationError, match="host must be"):
        EgressRule(host=host)


def test_policy_defaults_are_deny_and_intercept_all():
    p = EgressPolicy()
    assert p.default == "deny"
    assert p.intercept == "all"
    assert p.rules == []


def test_inject_must_reference_a_known_secret():
    with pytest.raises(ValidationError, match="unknown secret"):
        EgressConfigRequest(policy=EgressPolicy(rules=[EgressRule(host="github.com", inject="ghx")]), secrets={})


def test_valid_request_with_secret_round_trips():
    req = EgressConfigRequest(
        policy=EgressPolicy(rules=[EgressRule(host="github.com", inject="gh")]),
        secrets={"gh": {"header": "Authorization", "value": "Bearer t"}},
    )
    # SecretStr keeps the value out of repr/logs.
    assert "Bearer t" not in repr(req)
    assert req.secrets["gh"].value.get_secret_value() == "Bearer t"


def test_secret_header_accepts_valid_token():
    assert EgressSecret(header="PRIVATE-TOKEN", value="x").header == "PRIVATE-TOKEN"


def test_secret_header_rejects_empty():
    with pytest.raises(ValidationError):
        EgressSecret(header="", value="x")


@pytest.mark.parametrize("header", ["X-Bad\r\nInjected: 1", "X Token", "Authorization\n", "with space"])
def test_secret_header_rejects_non_token(header):
    """A header *name* is an RFC 7230 token. Whitespace or CR/LF could smuggle extra headers when the
    name is injected verbatim into the request (addon.request), so reject anything that isn't a token."""
    with pytest.raises(ValidationError, match="valid HTTP header name"):
        EgressSecret(header=header, value="x")


@pytest.mark.parametrize("value", ["tok\r\nX-Evil: 1", "tok\ninjected", "line1\rline2"])
def test_secret_value_rejects_crlf(value):
    """The value is injected verbatim into request headers (addon.request); CR/LF could smuggle extra
    headers. A real credential never contains control characters, so reject them at the boundary."""
    with pytest.raises(ValidationError, match="control characters"):
        EgressSecret(header="Authorization", value=value)


def test_to_sidecar_config_unwraps_secrets_and_dumps_policy():
    """to_sidecar_config() is the single authority for the wire->sidecar projection: it dumps the
    policy and unwraps SecretStr values into the {"policy", "secrets"} shape the sidecar parser reads."""
    req = EgressConfigRequest(
        policy=EgressPolicy(rules=[EgressRule(host="github.com", inject="gh")]),
        secrets={"gh": {"header": "Authorization", "value": "Bearer t"}},
    )
    cfg = req.to_sidecar_config()
    assert cfg["secrets"]["gh"] == {"header": "Authorization", "value": "Bearer t"}
    assert cfg["policy"]["rules"][0]["host"] == "github.com"
    assert cfg["policy"]["default"] == "deny"
