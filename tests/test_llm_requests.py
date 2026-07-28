"""Offline coverage for the Claude generator's request construction.

`AnthropicGenerator` has never run against the live API. These tests do not
change that — they cannot prove the server accepts the request. What they do is
exercise every line of the *client-side* code with a fake transport, so the
first paid call fails for an interesting reason rather than a typo.

Deliberately out of scope: whether the API accepts this combination of
`output_config`, streaming and the fallback beta. Only a real call settles that.
"""

from __future__ import annotations

import json

import anthropic
import httpx
import pytest

from src.ports.llm import (
    FALLBACK_BETA,
    FILE_BUNDLE_SCHEMA,
    MAX_TOKENS,
    PLAN_SCHEMA,
    AnthropicGenerator,
    GenerationMalformed,
    GenerationRefused,
)
from src.prd.schema import load_prd

PRD_PATH = "examples/todo_app.prd.json"


# --------------------------------------------------------------------------- #
# Fake transport
# --------------------------------------------------------------------------- #


class FakeBlock:
    def __init__(self, text): self.type, self.text = "text", text


class FakeMessage:
    def __init__(self, payload, stop_reason="end_turn", stop_details=None):
        self.content = [FakeBlock(json.dumps(payload))] if payload is not None else []
        self.stop_reason = stop_reason
        self.stop_details = stop_details


class FakeStream:
    def __init__(self, message): self._message = message
    def __enter__(self): return self
    def __exit__(self, *exc): return False
    def get_final_message(self): return self._message


class FakeSurface:
    def __init__(self, owner, beta): self._owner, self._beta = owner, beta

    def stream(self, **kwargs):
        self._owner.calls.append({"beta": self._beta, "kwargs": kwargs})
        outcome = self._owner.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return FakeStream(outcome)


class FakeClient:
    """Stands in for anthropic.Anthropic(), recording what it was asked for."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls: list[dict] = []
        self.messages = FakeSurface(self, beta=False)
        self.beta = type("Beta", (), {"messages": FakeSurface(self, beta=True)})()


def _bad_request(message):
    # anthropic's exception reaches into response.request, so a stub object
    # is not enough — it needs a real httpx.Response.
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.BadRequestError(
        message, response=httpx.Response(400, request=request), body=None
    )


@pytest.fixture
def prd():
    return load_prd(PRD_PATH)


def _gen(*outcomes):
    client = FakeClient(*outcomes)
    return AnthropicGenerator(client=client), client


# --------------------------------------------------------------------------- #
# Request shape
# --------------------------------------------------------------------------- #


def test_plan_request_is_well_formed(prd):
    gen, client = _gen(FakeMessage({"design_md": "# d", "pubspec": "name: x"}))
    gen.plan(prd)

    sent = client.calls[0]["kwargs"]
    assert sent["model"] == "claude-opus-5"
    assert sent["max_tokens"] == MAX_TOKENS
    assert sent["output_config"]["format"] == {"type": "json_schema", "schema": PLAN_SCHEMA}
    assert sent["output_config"]["effort"] == "high"
    assert sent["messages"][0]["role"] == "user"


def test_system_prompt_is_cached(prd):
    """The per-agent system prompt is stable across every call in a build."""
    gen, client = _gen(FakeMessage({"design_md": "d", "pubspec": "p"}))
    gen.plan(prd)

    system = client.calls[0]["kwargs"]["system"]
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert "Planning subagent" in system[0]["text"]


def test_fallback_beta_is_requested_and_uses_the_beta_surface(prd):
    gen, client = _gen(FakeMessage({"design_md": "d", "pubspec": "p"}))
    gen.plan(prd)

    assert client.calls[0]["beta"] is True
    assert client.calls[0]["kwargs"]["betas"] == [FALLBACK_BETA]
    assert client.calls[0]["kwargs"]["fallbacks"] == "default"


def test_progressive_disclosure_between_subagents(prd):
    """Each agent gets its own lane's guidance and not the other's.

    Note GenUI *does* name Firebase — to forbid it. Prohibiting a thing is not
    the same as being taught how to use it, so the check is for the concrete
    implementation guidance (`cloud_firestore`), not the brand name.
    """
    gen, client = _gen(FakeMessage({"files": []}), FakeMessage({"files": []}))
    gen.build_ui(prd, "# design", [])
    gen.wire_logic(prd, "# design", {}, [])

    genui_system = client.calls[0]["kwargs"]["system"][0]["text"]
    logic_system = client.calls[1]["kwargs"]["system"][0]["text"]

    # GenUI: told to stay out of the data layer, not told how to build one.
    assert "cloud_firestore" not in genui_system
    assert "no Firebase imports" in genui_system
    assert "lib/ui/ ONLY" in genui_system

    # Logic: owns the data layer, explicitly not the widget tree.
    assert "cloud_firestore" in logic_system
    assert "never the widget tree" in logic_system
    assert "Never declare a `Widget build(...)` method" in logic_system


def test_repair_pass_includes_the_diagnostics(prd):
    from src.ports.analyzer import Diagnostic

    gen, client = _gen(FakeMessage({"files": []}))
    d = Diagnostic("error", "lib/ui/a.dart", 7, "undefined_provider", "ghostProvider missing")
    gen.wire_logic(prd, "# design", {"lib/ui/a.dart": "..."}, [d])

    prompt = client.calls[0]["kwargs"]["messages"][0]["content"]
    assert "REPAIR PASS" in prompt
    assert "undefined_provider" in prompt and "ghostProvider missing" in prompt


def test_normal_pass_has_no_repair_framing(prd):
    gen, client = _gen(FakeMessage({"files": []}))
    gen.wire_logic(prd, "# design", {}, [])
    assert "REPAIR PASS" not in client.calls[0]["kwargs"]["messages"][0]["content"]


# --------------------------------------------------------------------------- #
# Response handling
# --------------------------------------------------------------------------- #


def test_refusal_raises(prd):
    detail = type("D", (), {"category": "policy"})()
    gen, _ = _gen(FakeMessage(None, stop_reason="refusal", stop_details=detail))
    with pytest.raises(GenerationRefused, match="policy"):
        gen.plan(prd)


def test_truncated_output_raises_rather_than_returning_half_a_file(prd):
    gen, _ = _gen(FakeMessage({"files": []}, stop_reason="max_tokens"))
    with pytest.raises(GenerationMalformed, match="max_tokens"):
        gen.plan(prd)


def test_unparseable_json_raises(prd):
    class Garbage(FakeMessage):
        def __init__(self):
            super().__init__({})
            self.content = [FakeBlock("{not json")]

    gen, _ = _gen(Garbage())
    with pytest.raises(GenerationMalformed, match="not valid JSON"):
        gen.plan(prd)


def test_empty_response_raises(prd):
    gen, _ = _gen(FakeMessage(None))
    with pytest.raises(GenerationMalformed, match="no text block"):
        gen.plan(prd)


# --------------------------------------------------------------------------- #
# Fallback-beta downgrade
# --------------------------------------------------------------------------- #


def test_downgrades_once_when_the_fallback_beta_is_not_enabled(prd):
    """Orgs without the beta must not lose the whole build to a 400."""
    gen, client = _gen(
        _bad_request("unsupported fallbacks parameter"),
        FakeMessage({"design_md": "d", "pubspec": "p"}),
    )
    gen.plan(prd)

    assert len(client.calls) == 2
    assert client.calls[0]["beta"] is True
    assert client.calls[1]["beta"] is False, "retry should use the non-beta surface"
    assert "fallbacks" not in client.calls[1]["kwargs"]


def test_downgrade_is_remembered_for_later_calls(prd):
    gen, client = _gen(
        _bad_request("fallbacks not allowed"),
        FakeMessage({"design_md": "d", "pubspec": "p"}),
        FakeMessage({"files": []}),
    )
    gen.plan(prd)
    gen.build_ui(prd, "# design", [])

    assert client.calls[2]["beta"] is False, "should not re-probe the beta every call"


def test_unrelated_bad_request_is_not_swallowed(prd):
    gen, _ = _gen(_bad_request("model: invalid model id"))
    with pytest.raises(anthropic.BadRequestError, match="invalid model id"):
        gen.plan(prd)


# --------------------------------------------------------------------------- #
# Lane handling
# --------------------------------------------------------------------------- #


def test_out_of_lane_files_are_returned_not_silently_dropped(prd):
    """Regression: `_bundle` used to filter these out, which meant the lane
    check in the graph nodes never fired and a model writing to the wrong lane
    produced a green build with its output quietly discarded."""
    gen, _ = _gen(FakeMessage({"files": [
        {"path": "lib/ui/home.dart", "content": "// ok"},
        {"path": "lib/providers/sneaky.dart", "content": "// wrong lane"},
    ]}))
    files = gen.build_ui(prd, "# design", [])

    assert "lib/providers/sneaky.dart" in files, "the node must get the chance to reject it"


def test_paths_are_normalised(prd):
    gen, _ = _gen(FakeMessage({"files": [
        {"path": "./lib/ui/a.dart", "content": "x"},
        {"path": "lib\\ui\\b.dart", "content": "y"},
    ]}))
    assert set(gen.build_ui(prd, "# design", [])) == {"lib/ui/a.dart", "lib/ui/b.dart"}


def test_analysis_options_is_not_asked_of_the_model(prd):
    """Fixed boilerplate; asking for it only invites drift."""
    gen, _ = _gen(FakeMessage({"design_md": "d", "pubspec": "p"}))
    plan = gen.plan(prd)
    assert "package:flutter_lints/flutter.yaml" in plan.analysis_options


def test_file_bundle_schema_is_strict():
    """additionalProperties must stay false or the model can invent fields."""
    assert FILE_BUNDLE_SCHEMA["additionalProperties"] is False
    assert FILE_BUNDLE_SCHEMA["properties"]["files"]["items"]["additionalProperties"] is False
    assert PLAN_SCHEMA["additionalProperties"] is False
