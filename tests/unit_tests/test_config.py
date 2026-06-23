import pytest
from pydantic import ValidationError

from daiv_sandbox.config import Settings, settings


def test_egress_enabled_false_without_ca():
    assert settings.egress_enabled is False


def test_egress_enabled_true_with_both_ca(monkeypatch):
    monkeypatch.setattr(settings, "EGRESS_CA_CERT_FILE", "/run/secrets/ca.crt")
    monkeypatch.setattr(settings, "EGRESS_CA_KEY_FILE", "/run/secrets/ca.key")
    assert settings.egress_enabled is True


def test_egress_ca_cert_without_key_fails_at_boot():
    with pytest.raises(ValidationError, match="EGRESS_CA"):
        Settings(EGRESS_CA_CERT_FILE="/run/secrets/ca.crt")


def test_egress_ca_key_without_cert_fails_at_boot():
    with pytest.raises(ValidationError, match="EGRESS_CA"):
        Settings(EGRESS_CA_KEY_FILE="/run/secrets/ca.key")


def test_egress_ca_both_set_constructs(monkeypatch):
    monkeypatch.setenv("DAIV_SANDBOX_EGRESS_CA_CERT_FILE", "/run/secrets/ca.crt")
    monkeypatch.setenv("DAIV_SANDBOX_EGRESS_CA_KEY_FILE", "/run/secrets/ca.key")
    s = Settings()
    assert s.egress_enabled is True


def test_reaper_defaults():
    assert settings.REAPER_ENABLED is True
    assert settings.REAPER_INTERVAL_SECONDS == 600
    assert settings.SESSION_GRACE_SECONDS == 43200
    assert settings.MAX_STOPPED_SESSIONS == 50
    assert settings.STOP_TIMEOUT_SECONDS == 2


def test_session_lock_defaults():
    # The 30s wait is load-bearing: it lets a client's concurrently-dispatched ops queue on the
    # per-session lock instead of failing fast with 409. A silent revert to a tiny value would
    # reintroduce the "Session is busy" crash under batched tool calls, so pin it here (test_locks.py
    # constructs the manager with explicit values and can't catch a default regression).
    assert settings.SESSION_LOCK_TTL_SECONDS == 900
    assert settings.SESSION_LOCK_WAIT_SECONDS == 30.0
    assert settings.SESSION_LOCK_REFRESH_SECONDS == 30.0


def test_dns_and_extra_hosts_defaults():
    assert settings.DNS == ["1.1.1.1", "8.8.8.8"]
    assert settings.EXTRA_HOSTS == []


def test_dns_and_extra_hosts_parse_comma_separated_env(monkeypatch):
    monkeypatch.setenv("DAIV_SANDBOX_DNS", "1.1.1.1, 8.8.8.8 ,9.9.9.9")
    monkeypatch.setenv("DAIV_SANDBOX_EXTRA_HOSTS", "gitlab, redis")
    parsed = Settings()
    assert parsed.DNS == ["1.1.1.1", "8.8.8.8", "9.9.9.9"]
    assert parsed.EXTRA_HOSTS == ["gitlab", "redis"]


def test_empty_extra_hosts_env_falls_back_to_default(monkeypatch):
    # env_ignore_empty=True: an empty value is treated as unset, keeping the default.
    monkeypatch.setenv("DAIV_SANDBOX_EXTRA_HOSTS", "")
    assert Settings().EXTRA_HOSTS == []


def test_fs_prune_dirs_default():
    # Pin the full default list so an accidental removal is caught; deliberate additions update this.
    assert settings.FS_PRUNE_DIRS == [
        ".git",
        ".hg",
        ".svn",
        ".idea",
        ".vs",
        "__pycache__",
        ".ruff_cache",
        ".mypy_cache",
        ".pytest_cache",
        ".pyre",
        ".pytype",
        ".hypothesis",
        ".ipynb_checkpoints",
        "*.egg-info",
        ".eggs",
        ".next",
        ".nuxt",
        ".svelte-kit",
        ".turbo",
        ".parcel-cache",
        ".angular",
        ".vite",
        ".astro",
        ".docusaurus",
        ".cache",
        ".phpunit.cache",
        ".gradle",
        "target",
        "obj",
    ]


def test_fs_prune_dirs_parse_comma_separated_env(monkeypatch):
    monkeypatch.setenv("DAIV_SANDBOX_FS_PRUNE_DIRS", ".git, node_modules ,__pycache__")
    assert Settings().FS_PRUNE_DIRS == [".git", "node_modules", "__pycache__"]


def test_egress_defaults():
    assert settings.EGRESS_PROXY_ENABLED is False
    assert settings.EGRESS_PROXY_PORT == 8080
    assert settings.EGRESS_PROXY_RUNTIME == "runc"
    assert settings.EGRESS_PROXY_NETWORK is None
    assert settings.EGRESS_CA_CERT_FILE is None
    assert settings.EGRESS_CA_KEY_FILE is None
    assert settings.EGRESS_PROXY_MEMORY_BYTES is None
    assert settings.EGRESS_PROXY_CPUS is None


def test_egress_proxy_image_default():
    assert settings.EGRESS_PROXY_IMAGE.endswith("daiv-sandbox-egress:latest")
