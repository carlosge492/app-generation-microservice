"""Claude-backed implementation of `CodeGenerator`.

Each subagent is a separate, single-shot structured-output call with its own
cached system prompt. Progressive disclosure is enforced here: the GenUI agent is
never shown Firebase/Riverpod guidance, and the Logic agent is never asked to
emit widgets. Path prefixes are re-checked after the fact — a model that writes
outside its lane has its stray files rejected rather than silently merged.
"""

from __future__ import annotations

import json
import os
from typing import Any

import anthropic

from src.ports.analyzer import Diagnostic
from src.ports.generator import Plan
from src.prd.schema import PRD

DEFAULT_MODEL = "claude-opus-5"
MAX_TOKENS = 64_000

# Server-side refusal fallback. Cheap insurance, but not every org has the beta
# enabled — `_call` downgrades once on a 400 rather than failing the build.
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class GenerationRefused(RuntimeError):
    """The model declined the request (stop_reason == 'refusal')."""


class GenerationMalformed(RuntimeError):
    """The model returned no parseable structured output."""


FILE_BUNDLE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "files": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["files"],
    "additionalProperties": False,
}

PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "design_md": {"type": "string"},
        "pubspec": {"type": "string"},
    },
    "required": ["design_md", "pubspec"],
    "additionalProperties": False,
}

PLANNING_SYSTEM = """\
You are the Planning subagent of an M2M Flutter app-generation pipeline.

Given a JSON PRD, produce exactly two artifacts:
1. DESIGN.md — the frozen specification every downstream agent reads. Cover the
   package name, data models, screens, and the ownership boundary between
   lib/ui/ (GenUI subagent) and lib/providers/ (Logic subagent).
2. pubspec.yaml — must declare name, description, environment, and dependencies
   on flutter, flutter_riverpod, firebase_core and cloud_firestore.

Constraints: standard Material/Cupertino widgets only. Riverpod for all state.
No custom painting, no complex animations — the CI pipeline must not fail.
DESIGN.md is frozen the moment you emit it; downstream agents may not revise it.
"""

GENUI_SYSTEM = """\
You are the GenUI subagent. You write the Flutter widget tree and nothing else.

Hard rules:
- Emit files under lib/ui/ ONLY. Never lib/providers/, never lib/main.dart.
- No business logic: no Firebase imports, no network calls, no persistence.
- No `setState`, except a strictly localized animation toggle, which must carry
  the trailing comment `// localized animation toggle`.
- Screens are `ConsumerWidget`. Read state via `ref.watch(<name>Provider)` and
  invoke behaviour via `ref.read(<name>Provider.notifier)`. You do NOT define
  those providers — the Logic subagent does. Use exactly the provider names
  given to you; inventing a name causes a build failure.
- Standard Material widgets only. Every file must be complete, parseable Dart
  with balanced braces.
- Always emit lib/ui/app.dart declaring the root MaterialApp and its routes.
"""

LOGIC_SYSTEM = """\
You are the Logic subagent. You own state and data access, never the widget tree.

Hard rules:
- Emit files under lib/providers/ plus lib/main.dart ONLY. Never lib/ui/.
- Never declare a `Widget build(...)` method — you do not build UI.
- Riverpod only. Declare every provider referenced by the UI you are shown,
  spelled exactly as the UI spells it.
- Firebase access goes through cloud_firestore inside providers/controllers.
- lib/main.dart is the composition root: ensureInitialized, Firebase.initializeApp,
  then runApp inside a ProviderScope.
- Every file must be complete, parseable Dart with balanced braces.

When you are given QA diagnostics, this is a REPAIR pass. Patch the specific
callbacks and providers named in the diagnostics. Do not rewrite unrelated files.
"""


class AnthropicGenerator:
    def __init__(self, model: str = DEFAULT_MODEL, effort: str = "high") -> None:
        self.model = os.getenv("SUPERVISOR_MODEL", model)
        self.effort = effort
        self._client = anthropic.Anthropic()
        self._fallbacks_supported = True

    # -- transport ---------------------------------------------------------- #

    def _call(self, system: str, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        kwargs: dict[str, Any] = dict(
            model=self.model,
            max_tokens=MAX_TOKENS,
            system=[{
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": prompt}],
            output_config={
                "effort": self.effort,
                "format": {"type": "json_schema", "schema": schema},
            },
        )
        if self._fallbacks_supported:
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"

        try:
            message = self._stream(kwargs)
        except anthropic.BadRequestError as exc:
            if not self._fallbacks_supported or "fallback" not in str(exc).lower():
                raise
            # Beta not enabled for this org — proceed without refusal fallback.
            self._fallbacks_supported = False
            kwargs.pop("betas", None)
            kwargs.pop("fallbacks", None)
            message = self._stream(kwargs)

        if message.stop_reason == "refusal":
            detail = getattr(message, "stop_details", None)
            raise GenerationRefused(
                f"model declined the request (category="
                f"{getattr(detail, 'category', None)!r})"
            )
        if message.stop_reason == "max_tokens":
            raise GenerationMalformed(
                f"output hit max_tokens ({MAX_TOKENS}); the PRD is too large for one pass"
            )

        text = next((b.text for b in message.content if b.type == "text"), None)
        if not text:
            raise GenerationMalformed("response contained no text block")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise GenerationMalformed(f"structured output was not valid JSON: {exc}") from exc

    def _stream(self, kwargs: dict[str, Any]):
        # Streaming is required at this max_tokens; the helper accumulates for us.
        surface = self._client.beta.messages if "betas" in kwargs else self._client.messages
        with surface.stream(**kwargs) as stream:
            return stream.get_final_message()

    @staticmethod
    def _bundle(payload: dict[str, Any], allowed_prefixes: tuple[str, ...]) -> dict[str, str]:
        files: dict[str, str] = {}
        for entry in payload.get("files", []):
            path = entry["path"].lstrip("./")
            if not path.startswith(allowed_prefixes):
                # Lane violation. Drop it rather than let it cross the boundary.
                continue
            files[path] = entry["content"]
        return files

    # -- subagents ---------------------------------------------------------- #

    def plan(self, prd: PRD) -> Plan:
        payload = self._call(
            PLANNING_SYSTEM,
            "PRD:\n```json\n" + prd.model_dump_json(indent=2) + "\n```",
            PLAN_SCHEMA,
        )
        # analysis_options.yaml is fixed boilerplate, not a creative artifact —
        # asking the model for it only invites drift.
        from src.ports.templates import ANALYSIS_OPTIONS

        return Plan(
            design_md=payload["design_md"],
            pubspec=payload["pubspec"],
            analysis_options=ANALYSIS_OPTIONS,
        )

    def build_ui(
        self, prd: PRD, design_md: str, diagnostics: list[Diagnostic] | None = None
    ) -> dict[str, str]:
        prompt = (
            "DESIGN.md (frozen):\n```markdown\n" + design_md + "\n```\n\n"
            "PRD:\n```json\n" + prd.model_dump_json(indent=2) + "\n```\n\n"
            "Emit every screen plus lib/ui/app.dart."
        )
        if diagnostics:
            prompt += (
                "\n\n## REPAIR PASS — QA reported these diagnostics against your files\n\n"
                + "\n".join(f"- {d.render()}" for d in diagnostics)
                + "\n\nRe-emit only the files these diagnostics name, fixed."
            )
        payload = self._call(GENUI_SYSTEM, prompt, FILE_BUNDLE_SCHEMA)
        return self._bundle(payload, ("lib/ui/",))

    def wire_logic(
        self,
        prd: PRD,
        design_md: str,
        ui_files: dict[str, str],
        diagnostics: list[Diagnostic],
    ) -> dict[str, str]:
        ui_dump = "\n\n".join(
            f"### {path}\n```dart\n{content}\n```" for path, content in sorted(ui_files.items())
        )
        prompt = (
            "DESIGN.md (frozen):\n```markdown\n" + design_md + "\n```\n\n"
            "PRD:\n```json\n" + prd.model_dump_json(indent=2) + "\n```\n\n"
            "Widget tree written by the GenUI subagent:\n\n" + ui_dump
        )
        if diagnostics:
            prompt += (
                "\n\n## REPAIR PASS — QA reported these diagnostics\n\n"
                + "\n".join(f"- {d.render()}" for d in diagnostics)
                + "\n\nPatch only what these diagnostics name."
            )
        payload = self._call(LOGIC_SYSTEM, prompt, FILE_BUNDLE_SCHEMA)
        return self._bundle(payload, ("lib/providers/", "lib/main.dart"))
