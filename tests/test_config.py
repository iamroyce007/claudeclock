"""Configuration loading, validation, and the re-arm command construction."""

from __future__ import annotations

import pytest

from claudeclock.config import Config, ConfigError


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Config reads os.environ; make each test start from a blank slate."""
    for key in list(__import__("os").environ):
        if key.startswith("CLAUDECLOCK_"):
            monkeypatch.delenv(key, raising=False)


def load(monkeypatch, tmp_path, **env):
    monkeypatch.setenv("CLAUDECLOCK_STATE_DIR", str(tmp_path))
    for key, value in env.items():
        monkeypatch.setenv(key, str(value))
    return Config.load()


# --------------------------------------------------------------------------
# defaults & derived paths
# --------------------------------------------------------------------------


def test_defaults_are_sane(monkeypatch, tmp_path):
    config = load(monkeypatch, tmp_path)
    assert config.window_hours == 5.0
    assert config.window_seconds == 18000.0
    assert config.alert_thresholds == (30, 10, 5)
    assert config.sources == ("statusline", "oauth", "local")
    assert config.auto_trigger is True


def test_state_paths_live_under_the_state_dir(monkeypatch, tmp_path):
    config = load(monkeypatch, tmp_path)
    assert config.log_file.parent == tmp_path
    assert config.event_log_file.parent == tmp_path
    assert config.state_file.parent == tmp_path


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------


def test_unknown_source_is_rejected(monkeypatch, tmp_path):
    with pytest.raises(ConfigError, match="unknown source"):
        load(monkeypatch, tmp_path, CLAUDECLOCK_SOURCES="oauth,telepathy")


def test_empty_source_list_is_rejected(monkeypatch, tmp_path):
    with pytest.raises(ConfigError, match="at least one source"):
        load(monkeypatch, tmp_path, CLAUDECLOCK_SOURCES=",")


def test_non_numeric_interval_is_rejected(monkeypatch, tmp_path):
    with pytest.raises(ConfigError, match="must be a number"):
        load(monkeypatch, tmp_path, CLAUDECLOCK_POLL_INTERVAL="soon")


def test_absurd_poll_interval_is_rejected(monkeypatch, tmp_path):
    """The usage endpoint rate-limits aggressive polling, so there is a floor."""
    with pytest.raises(ConfigError, match=">= 30"):
        load(monkeypatch, tmp_path, CLAUDECLOCK_POLL_INTERVAL="0.1")


def test_bad_boolean_is_rejected(monkeypatch, tmp_path):
    with pytest.raises(ConfigError, match="must be a boolean"):
        load(monkeypatch, tmp_path, CLAUDECLOCK_AUTO_TRIGGER="maybe")


def test_non_numeric_threshold_is_rejected(monkeypatch, tmp_path):
    with pytest.raises(ConfigError, match="comma-separated integers"):
        load(monkeypatch, tmp_path, CLAUDECLOCK_ALERT_THRESHOLDS="30,soon,5")


def test_negative_threshold_is_rejected(monkeypatch, tmp_path):
    with pytest.raises(ConfigError, match="must be positive"):
        load(monkeypatch, tmp_path, CLAUDECLOCK_ALERT_THRESHOLDS="30,-5")


def test_inverted_backoff_bounds_are_rejected(monkeypatch, tmp_path):
    with pytest.raises(ConfigError, match="BACKOFF_MAX"):
        load(monkeypatch, tmp_path, CLAUDECLOCK_BACKOFF_MIN="100", CLAUDECLOCK_BACKOFF_MAX="10")


def test_bad_log_level_is_rejected(monkeypatch, tmp_path):
    with pytest.raises(ConfigError, match="LOG_LEVEL"):
        load(monkeypatch, tmp_path, CLAUDECLOCK_LOG_LEVEL="chatty")


def test_missing_config_file_is_reported(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        Config.load(tmp_path / "nope.env")


# --------------------------------------------------------------------------
# normalisation
# --------------------------------------------------------------------------


def test_thresholds_are_deduped_and_ordered(monkeypatch, tmp_path):
    config = load(monkeypatch, tmp_path, CLAUDECLOCK_ALERT_THRESHOLDS="5, 30,10 ,10,5")
    assert config.alert_thresholds == (30, 10, 5)


def test_blank_values_fall_back_to_defaults(monkeypatch, tmp_path):
    config = load(monkeypatch, tmp_path, CLAUDECLOCK_TRIGGER_PROMPT="", CLAUDECLOCK_POLL_INTERVAL="  ")
    assert config.trigger_prompt == "Hi"
    assert config.poll_interval == 600.0


def test_verbose_forces_debug_level(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDECLOCK_STATE_DIR", str(tmp_path))
    assert Config.load(verbose=True).log_level == "DEBUG"


# --------------------------------------------------------------------------
# re-arm command construction
# --------------------------------------------------------------------------


def test_trigger_argv_substitutes_the_prompt(monkeypatch, tmp_path):
    config = load(monkeypatch, tmp_path)
    argv = config.trigger_argv()
    assert argv[0] == "claude"
    assert "Hi" in argv
    assert "--model" in argv


def test_multiword_prompt_stays_one_argument(monkeypatch, tmp_path):
    """A prompt with spaces must not split into several argv entries."""
    config = load(
        monkeypatch, tmp_path, CLAUDECLOCK_TRIGGER_PROMPT="please start a new session"
    )
    argv = config.trigger_argv()
    assert "please start a new session" in argv


def test_prompt_is_never_shell_interpreted(monkeypatch, tmp_path):
    """Shell metacharacters are inert: argv is passed without a shell."""
    nasty = "hi; rm -rf /"
    config = load(monkeypatch, tmp_path, CLAUDECLOCK_TRIGGER_PROMPT=nasty)
    argv = config.trigger_argv()
    assert nasty in argv
    assert not any(part == "rm" for part in argv)


def test_explicit_model_is_not_duplicated(monkeypatch, tmp_path):
    config = load(
        monkeypatch,
        tmp_path,
        CLAUDECLOCK_TRIGGER_COMMAND="claude -p {prompt} --model custom-model",
    )
    argv = config.trigger_argv()
    assert argv.count("--model") == 1
    assert "custom-model" in argv
