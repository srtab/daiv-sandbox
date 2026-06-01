from daiv_sandbox.config import settings


def test_reaper_defaults():
    assert settings.REAPER_ENABLED is True
    assert settings.REAPER_INTERVAL_SECONDS == 600
    assert settings.SESSION_GRACE_SECONDS == 43200
    assert settings.MAX_STOPPED_SESSIONS == 50
    assert settings.STOP_TIMEOUT_SECONDS == 2
