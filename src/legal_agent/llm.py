"""The only place that talks to the LLM: `claude -p` (Claude Code in print mode).

Each call sends one system prompt + one user prompt and gets back JSON that
matches a JSON Schema (`--json-schema`), so the agents never parse free text.

Credentials: if CLAUDE_KEY is set it is passed to the CLI as
CLAUDE_CODE_OAUTH_TOKEN (the variable `claude` reads for a token from
`claude setup-token`). Otherwise the CLI's own login is used. If neither works,
or the CLI is missing, LLMUnavailable is raised with a message saying what to fix.
There is deliberately no offline or replay fallback.
"""

import json
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass

DEFAULT_MODEL = "sonnet"
TIMEOUT_SECONDS = 180


class LLMUnavailable(RuntimeError):
    """`claude -p` cannot be run: CLI missing, bad credentials, or an API error."""


@dataclass
class LLMResult:
    data: dict  # the structured output, already matching the schema
    input_tokens: int
    output_tokens: int
    cost_usd: float
    seconds: float


def ask_claude(system: str, prompt: str, schema: dict, model: str = DEFAULT_MODEL) -> LLMResult:
    command = [
        "claude", "-p",
        "--model", model,
        "--system-prompt", system,
        "--json-schema", json.dumps(schema),
        "--output-format", "json",
        "--tools", "",  # no file, shell or web tools: the model only answers
        "--strict-mcp-config",  # and no MCP servers from the machine's config
        "--setting-sources", "",  # ignore user and project settings
        "--no-session-persistence",
    ]
    env = dict(os.environ)
    if os.environ.get("CLAUDE_KEY"):
        env["CLAUDE_CODE_OAUTH_TOKEN"] = os.environ["CLAUDE_KEY"]

    start = time.monotonic()
    try:
        # Run from an empty temp dir so no CLAUDE.md from this repo leaks into the prompt.
        completed = subprocess.run(
            command, input=prompt, capture_output=True, text=True,
            env=env, cwd=tempfile.gettempdir(), timeout=TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        raise LLMUnavailable(
            "The `claude` CLI was not found. Install Claude Code "
            "(npm install -g @anthropic-ai/claude-code) and set CLAUDE_KEY."
        ) from None
    except subprocess.TimeoutExpired:
        raise LLMUnavailable(f"`claude -p` did not answer within {TIMEOUT_SECONDS} seconds.") from None

    try:
        reply = json.loads(completed.stdout)
    except json.JSONDecodeError:
        reply = {"is_error": True}
    if completed.returncode != 0 or reply.get("is_error"):
        detail = (reply.get("result") or completed.stderr or completed.stdout).strip()[-500:]
        raise LLMUnavailable(f"`claude -p` failed. Check that CLAUDE_KEY holds a valid key. Details: {detail}")
    if not isinstance(reply.get("structured_output"), dict):
        raise ValueError(f"`claude -p` returned no structured output: {reply.get('result', '')[:300]}")

    usage = reply.get("usage", {})
    return LLMResult(
        data=reply["structured_output"],
        input_tokens=usage.get("input_tokens", 0)
        + usage.get("cache_creation_input_tokens", 0)
        + usage.get("cache_read_input_tokens", 0),
        output_tokens=usage.get("output_tokens", 0),
        cost_usd=reply.get("total_cost_usd", 0.0),
        seconds=time.monotonic() - start,
    )
