"""Re-arm subprocess behaviour, driven against fake `claude` executables."""

from __future__ import annotations

import sys
import textwrap

import pytest

from claudeclock.config import Config
from claudeclock.trigger import TriggerError, _extract_session_id, _is_usage_limited, send_trigger


def make_fake_claude(tmp_path, body: str, name: str = "fake_claude.py") -> str:
    """Write a Python stub that stands in for the `claude` CLI.

    Returns a command *prefix* that runs it via the current interpreter rather
    than relying on a shebang: shebang lines cannot handle spaces in the
    interpreter path (and do not exist on Windows at all).
    """
    path = tmp_path / name
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return f'"{sys.executable}" "{path}"'


def config_for(tmp_path, command: str, **kwargs) -> Config:
    defaults = dict(
        state_dir=tmp_path,
        trigger_command=command,
        trigger_model=None,
        trigger_timeout=30.0,
        trigger_max_retries=2,
        backoff_min=0.01,
        backoff_max=0.02,
    )
    defaults.update(kwargs)
    return Config(**defaults)


# --------------------------------------------------------------------------
# happy path
# --------------------------------------------------------------------------


def test_successful_trigger(tmp_path):
    fake = make_fake_claude(
        tmp_path,
        """
        import json, sys
        print(json.dumps({"session_id": "abc-123", "result": "Hello!"}))
        sys.exit(0)
        """,
    )
    config = config_for(tmp_path, f"{fake} -p {{prompt}}")
    result = send_trigger(config)

    assert result.ok
    assert result.attempts == 1
    assert result.session_id == "abc-123"


def test_prompt_reaches_the_child_as_one_argument(tmp_path):
    """A multi-word prompt must arrive intact, not split across argv."""
    marker = tmp_path / "argv.txt"
    fake = make_fake_claude(
        tmp_path,
        f"""
        import sys
        open({str(marker)!r}, "w").write("\\n".join(sys.argv[1:]))
        print("{{}}")
        """,
    )
    config = config_for(
        tmp_path, f"{fake} -p {{prompt}}", trigger_prompt="hello there friend"
    )
    result = send_trigger(config)

    assert result.ok
    assert "hello there friend" in marker.read_text().splitlines()


def test_shell_metacharacters_are_inert(tmp_path):
    """The prompt is argv, never a shell string."""
    marker = tmp_path / "argv.txt"
    canary = tmp_path / "pwned.txt"
    fake = make_fake_claude(
        tmp_path,
        f"""
        import sys
        open({str(marker)!r}, "w").write("\\n".join(sys.argv[1:]))
        print("{{}}")
        """,
    )
    config = config_for(
        tmp_path,
        f"{fake} -p {{prompt}}",
        trigger_prompt=f"hi; touch {canary}",
    )
    result = send_trigger(config)

    assert result.ok
    assert not canary.exists(), "prompt was interpreted by a shell"
    assert f"hi; touch {canary}" in marker.read_text()


# --------------------------------------------------------------------------
# failure handling
# --------------------------------------------------------------------------


def test_failure_is_retried_then_reported(tmp_path):
    counter = tmp_path / "count.txt"
    fake = make_fake_claude(
        tmp_path,
        f"""
        import sys, os
        p = {str(counter)!r}
        n = int(open(p).read()) if os.path.exists(p) else 0
        open(p, "w").write(str(n + 1))
        sys.stderr.write("connection reset")
        sys.exit(1)
        """,
    )
    config = config_for(tmp_path, f"{fake} -p {{prompt}}", trigger_max_retries=2)
    result = send_trigger(config)

    assert not result.ok
    assert result.attempts == 3, "should be one initial attempt plus two retries"
    assert int(counter.read_text()) == 3


def test_transient_failure_then_success(tmp_path):
    counter = tmp_path / "count.txt"
    fake = make_fake_claude(
        tmp_path,
        f"""
        import sys, os, json
        p = {str(counter)!r}
        n = int(open(p).read()) if os.path.exists(p) else 0
        open(p, "w").write(str(n + 1))
        if n == 0:
            sys.stderr.write("network unreachable")
            sys.exit(1)
        print(json.dumps({{"session_id": "recovered"}}))
        """,
    )
    config = config_for(tmp_path, f"{fake} -p {{prompt}}")
    result = send_trigger(config)

    assert result.ok
    assert result.attempts == 2
    assert result.session_id == "recovered"


def test_usage_limit_aborts_instead_of_burning_retries(tmp_path):
    """If the window has not really reset, retrying is pointless and costly."""
    counter = tmp_path / "count.txt"
    fake = make_fake_claude(
        tmp_path,
        f"""
        import sys, os
        p = {str(counter)!r}
        n = int(open(p).read()) if os.path.exists(p) else 0
        open(p, "w").write(str(n + 1))
        sys.stderr.write("Usage limit reached. Your limit will reset at 5pm.")
        sys.exit(1)
        """,
    )
    config = config_for(tmp_path, f"{fake} -p {{prompt}}", trigger_max_retries=5)
    result = send_trigger(config)

    assert not result.ok
    assert result.attempts == 1, "should not retry a genuine usage limit"
    assert int(counter.read_text()) == 1
    assert "usage limit" in result.detail.lower()


def test_timeout_is_handled(tmp_path):
    fake = make_fake_claude(
        tmp_path,
        """
        import time
        time.sleep(30)
        """,
    )
    config = config_for(
        tmp_path, f"{fake} -p {{prompt}}", trigger_timeout=5.0, trigger_max_retries=0
    )
    result = send_trigger(config)

    assert not result.ok
    assert "timed out" in result.detail


def test_missing_executable_is_a_clear_error(tmp_path):
    config = config_for(tmp_path, "definitely-not-a-real-binary-xyz -p {prompt}")
    with pytest.raises(TriggerError, match="could not find"):
        send_trigger(config)


# --------------------------------------------------------------------------
# output parsing
# --------------------------------------------------------------------------


def test_session_id_from_plain_json():
    assert _extract_session_id('{"session_id": "s-1", "result": "hi"}') == "s-1"


def test_session_id_from_stream_json():
    """`--output-format stream-json` emits one object per line."""
    stream = (
        '{"type": "system", "session_id": "s-2"}\n'
        '{"type": "result", "session_id": "s-2", "result": "hi"}\n'
    )
    assert _extract_session_id(stream) == "s-2"


@pytest.mark.parametrize("blob", ["", "not json at all", "{}", "[]", "null"])
def test_session_id_absent_is_not_an_error(blob):
    assert _extract_session_id(blob) is None


@pytest.mark.parametrize(
    "text",
    [
        "Usage limit reached",
        "usage limit reached, resets at 5pm",
        "Error: rate limit exceeded",
        "HTTP 429 Too Many Requests",
        "your limit will reset at 17:00",
    ],
)
def test_usage_limit_is_recognised(text):
    assert _is_usage_limited("", text)


@pytest.mark.parametrize(
    "text", ["connection reset by peer", "ENOTFOUND api.anthropic.com", ""]
)
def test_ordinary_errors_are_not_usage_limits(text):
    assert not _is_usage_limited("", text)
