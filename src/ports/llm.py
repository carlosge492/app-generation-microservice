"""Claude-backed implementation of `CodeGenerator`.

    STATUS: exercised against the live API on Haiku 4.5. Structured output
    parses, the request shape is accepted, and a generated app has passed real
    `flutter analyze` plus its widget smoke tests. Not yet run on Opus, and not
    yet green across the whole eval sweep — treat the pass rate, not this
    docstring, as the measure.

    What the first live sweeps taught, all encoded elsewhere in the pipeline:
    `effort` is rejected outright by models that lack it (hence
    OPTIONAL_FEATURES); models write lib/models/ and lib/firebase_options.dart
    because that is ordinary Flutter, so lane rules must accommodate it; and a
    lane violation is worth reporting as a repairable diagnostic rather than
    failing the build outright.

    `TemplateGenerator` remains the deterministic reference path and is
    validated end to end. Compare against it with
    `evals/run.py --generator claude --analyzer dart --run-tests`.

Design intent (as written, not as proven):

Each subagent is a separate, single-shot structured-output call with its own
cached system prompt. Progressive disclosure is enforced here: the GenUI agent is
never shown Firebase/Riverpod guidance, and the Logic agent is never asked to
emit widgets. Lane enforcement is left to the graph nodes rather than done here —
see `_bundle` for why filtering in this module was actively harmful.
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

# Request features we prefer but can live without. If the server rejects one
# by name, it is dropped and the call retried. Order matters only for the
# retry budget in `_call`.
OPTIONAL_FEATURES = ("effort", "fallbacks")


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
- Emit files under lib/ui/ ONLY. Everything else under lib/ belongs to the Logic
  subagent: lib/main.dart, lib/providers/, lib/models/, lib/services/ and
  lib/firebase_options.dart. Do not write them even if the app obviously needs
  them — emitting one fails the build.
- Do NOT declare `main()` or the root App widget. Logic owns the composition
  root; a second copy in lib/ui/ leaves the app with two shells.
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
- You own everything under lib/ EXCEPT lib/ui/. Put state in lib/providers/,
  data classes in lib/models/, Firestore access in lib/services/ if you want a
  service layer, and the composition root in lib/main.dart. Use the ordinary
  Flutter layout; you are not restricted to one directory.
- NEVER write anything under lib/ui/ — that is the GenUI subagent's, and it has
  already written the widget tree you are being shown.
- Never declare a `Widget build(...)` method — you do not build UI.
- Declare the root App widget and `main()` exactly once, in lib/main.dart.
- Riverpod only. Declare every provider referenced by the UI you are shown,
  spelled exactly as the UI spells it.
- Declare NO provider the UI does not reference. A derived "convenience"
  provider that no screen watches is dead code and fails the build, however
  reasonable it looks — if a screen needs a count, let that screen derive it.
- Inside a Notifier/AsyncNotifier/StateNotifier subclass, never name a method
  after one the Riverpod base class already declares: `update`, `build`,
  `state`, `ref`, `future`, `stream`, `mounted`, `listenSelf`, `onDispose`.
  `update` is the trap — the base class signature is
  `update(FutureOr<State> Function(State), {onError})`, so a domain method
  `update(String id, {...})` is an invalid override and will not compile. Use a
  domain-specific name: `updateRecord`, `saveDraft`, `renameItem`.
- Firestore has no date type. A `DateTime` is stored as a `Timestamp` and comes
  back as a `Timestamp`, so `data['x'] as DateTime?` throws
  `type 'Timestamp' is not a subtype of type 'DateTime?'` the first time the app
  reads back a document it wrote. Deserialise with
  `(data['x'] as Timestamp?)?.toDate()`. This is the trap that type-checks:
  `flutter analyze` is clean and the widget tests pass, because catching it
  needs a live Firestore. Whatever you choose, read it back the way you wrote
  it: storing an ISO string and parsing it back is fine, and so is a Timestamp,
  but `DateTime.tryParse('${data['x']}')` against a field you wrote as a
  Timestamp parses the *stringified* Timestamp, fails, and silently yields your
  fallback rather than throwing.
- Firebase access goes through cloud_firestore inside providers/controllers.
- lib/main.dart is the composition root: ensureInitialized, Firebase.initializeApp,
  then runApp inside a ProviderScope.
- Every file must be complete, parseable Dart with balanced braces.

When you are given QA diagnostics, this is a REPAIR pass. Patch the specific
callbacks and providers named in the diagnostics. Do not rewrite unrelated files.
"""


class AnthropicGenerator:
    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        effort: str = "high",
        client: Any | None = None,
    ) -> None:
        self.model = os.getenv("SUPERVISOR_MODEL", model)
        self.effort = effort
        # `client` exists so the request shape can be exercised without
        # credentials; anthropic.Anthropic() raises at construction without them.
        # `overloaded_error` (529) is transient and cost one PRD of a sweep
        # to a single unlucky moment. The SDK retries 5xx already; this just
        # buys more headroom than the default of 2.
        self._client = (
            client if client is not None else anthropic.Anthropic(max_retries=6)
        )
        # Populated at runtime from the server's own 400s.
        self._unsupported: set[str] = set()

    # -- transport ---------------------------------------------------------- #

    def _build_kwargs(self, system: str, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        output_config: dict[str, Any] = {
            "format": {"type": "json_schema", "schema": schema},
        }
        if "effort" not in self._unsupported:
            output_config["effort"] = self.effort

        kwargs: dict[str, Any] = dict(
            model=self.model,
            max_tokens=MAX_TOKENS,
            system=[{
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": prompt}],
            output_config=output_config,
        )
        if "fallbacks" not in self._unsupported:
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
        return kwargs

    @staticmethod
    def _rejected_feature(error: str) -> str | None:
        """Which optional request feature did the server refuse, if any."""
        lowered = error.lower()
        for feature in OPTIONAL_FEATURES:
            if feature in lowered:
                return feature
        return None

    def _call(self, system: str, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        # `effort` and the refusal-fallback beta are quality features, not
        # requirements: `effort` is rejected outright by models that lack it
        # (Haiku 4.5 returns "This model does not support the effort parameter")
        # and the beta is not enabled for every org. Losing a whole build to
        # either would be absurd, so drop whichever the server names and retry.
        # Each is remembered, so the probe costs one rejected request per
        # generator, not one per call.
        for _ in range(len(OPTIONAL_FEATURES) + 1):
            kwargs = self._build_kwargs(system, prompt, schema)
            try:
                message = self._stream(kwargs)
                break
            except anthropic.BadRequestError as exc:
                feature = self._rejected_feature(str(exc))
                if feature is None or feature in self._unsupported:
                    raise  # a real problem, not a capability mismatch
                self._unsupported.add(feature)
        else:  # pragma: no cover - exhausted every downgrade
            raise GenerationMalformed("could not construct an acceptable request")

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
    def _bundle(payload: dict[str, Any]) -> dict[str, str]:
        """Normalise paths. Deliberately does NOT filter by lane.

        An earlier version dropped out-of-lane files here. That silently
        defeated the enforcement in `make_genui_node` / `make_logic_node`: by the
        time the node looked for stray paths there were none left, so a model
        writing into the wrong lane produced a green build with its output
        quietly discarded — the failure mode hardest to notice and worst to ship.

        Returning everything lets the node fail the build and name the offending
        paths, which is what it was written to do.
        """
        return {
            entry["path"].lstrip("./").replace("\\", "/"): entry["content"]
            for entry in payload.get("files", [])
        }

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
        return self._bundle(payload)

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
        return self._bundle(payload)
