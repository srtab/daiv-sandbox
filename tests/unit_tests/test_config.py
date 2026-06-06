from daiv_sandbox.config import Settings, settings


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
