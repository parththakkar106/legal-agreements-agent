import json
import subprocess

import pytest

from legal_agent import llm


def fake_run(stdout="", returncode=0, stderr="", seen=None):
    def run(command, **kwargs):
        if seen is not None:
            seen.update(command=command, **kwargs)
        return subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr=stderr)
    return run


def reply(**extra):
    base = {"is_error": False, "structured_output": {"topic": "Governing Law"}, "total_cost_usd": 0.01,
            "usage": {"input_tokens": 2, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 0, "output_tokens": 20}}
    return json.dumps({**base, **extra})


def test_returns_structured_output_and_usage(monkeypatch):
    monkeypatch.setattr(subprocess, "run", fake_run(reply()))

    result = llm.ask_claude("system", "prompt", {"type": "object"})

    assert result.data == {"topic": "Governing Law"}
    assert (result.input_tokens, result.output_tokens, result.cost_usd) == (102, 20, 0.01)


def test_claude_key_is_passed_as_oauth_token_and_prompt_on_stdin(monkeypatch):
    seen = {}
    monkeypatch.setenv("CLAUDE_KEY", "secret")
    monkeypatch.setattr(subprocess, "run", fake_run(reply(), seen=seen))

    llm.ask_claude("system", "the prompt", {"type": "object"})

    assert seen["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "secret"
    assert seen["input"] == "the prompt"
    assert seen["command"][:2] == ["claude", "-p"]


def test_missing_cli_raises_clear_error(monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError
    monkeypatch.setattr(subprocess, "run", missing)

    with pytest.raises(llm.LLMUnavailable, match="CLI was not found"):
        llm.ask_claude("system", "prompt", {})


def test_api_error_raises_clear_error(monkeypatch):
    monkeypatch.setattr(subprocess, "run", fake_run(json.dumps({"is_error": True, "result": "Invalid API key"}), 1))

    with pytest.raises(llm.LLMUnavailable, match="CLAUDE_KEY"):
        llm.ask_claude("system", "prompt", {})


def test_missing_structured_output_is_a_value_error(monkeypatch):
    monkeypatch.setattr(subprocess, "run", fake_run(reply(structured_output=None, result="sorry")))

    with pytest.raises(ValueError):
        llm.ask_claude("system", "prompt", {})
