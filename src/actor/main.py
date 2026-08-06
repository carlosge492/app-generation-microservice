"""Apify Actor entry point: the same pipeline, a different front door.

Nothing about how an app gets built lives here. This reads Apify's input,
hands it to `build_graph` exactly as `src/supervisor.py` does, and writes what
comes back into Apify's storages. Every decision that matters — how the four
subagents divide the work, how diagnostics route back to whichever agent owns
them, what counts as clean — is unchanged and shared.

The one thing that is genuinely different is how payment is proven. On the
hosted service an x402 authorization settles on-chain before packaging; here
Apify is the payment rail, so the Actor charges a pay-per-event and only then
sets `x402_payment_verified`. CLAUDE.md's rule is that packaging never runs
without that flag being earned, and it is earned here by a charge that
succeeded, not by being hardcoded true. A charge that fails leaves the flag
False, which the packaging node itself refuses on — the same refusal the hosted
service relies on.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path
from typing import Any

from apify import Actor

from src.build.pipeline import BuildResult  # noqa: F401  (imported for typing parity)
from src.graph.builder import build_graph
from src.graph.state import all_files, initial_state
from src.ports.analyzer import get_analyzer
from src.ports.generator import get_generator
from src.prd.schema import PRD

log = logging.getLogger(__name__)

BUILD_DIR = Path("/tmp/actor-build")
# Charged only when an APK is actually requested. Generation is deliberately
# free: someone deciding whether this is worth paying for should be able to read
# the code it writes for their own spec first.
APK_EVENT = "apk-build"


def _source_records(files: dict[str, str]) -> list[dict[str, Any]]:
    """One dataset record per generated file, biggest first.

    Ordered so the dataset preview shows the widget tree and providers rather
    than starting with analysis_options.yaml.
    """
    def kind_of(path: str) -> str:
        if path.startswith("lib/ui/"):
            return "ui"
        if path.startswith("lib/"):
            return "state"
        if path.startswith(("test/", "integration_test/", "test_driver/")):
            return "test"
        return "project"

    records = [
        {"path": path, "kind": kind_of(path), "bytes": len(content), "content": content}
        for path, content in files.items()
    ]
    records.sort(key=lambda record: (record["kind"], -record["bytes"]))
    return records


async def main() -> None:
    async with Actor:
        raw = await Actor.get_input() or {}

        try:
            prd = PRD.model_validate(raw.get("prd") or {})
        except Exception as exc:
            await Actor.fail(
                status_message=f"The PRD did not validate, so nothing was built: {exc}"
            )
            return

        api_key = (raw.get("anthropicApiKey") or "").strip()
        want_apk = bool(raw.get("packageApk"))

        # A key means the real generator; without one the template generator
        # still produces a compiling app, which is a more useful default than
        # failing on a missing credential.
        if api_key:
            os.environ["ANTHROPIC_API_KEY"] = api_key
            generator_kind = "claude"
            generator_kwargs = {"model": raw.get("model") or "claude-sonnet-5"}
        else:
            generator_kind = "template"
            generator_kwargs = {}
            Actor.log.info(
                "No Anthropic key supplied — using the template generator. "
                "The output compiles but is not model-designed."
            )

        # --- payment, before packaging and not after --------------------------
        paid = False
        if want_apk:
            try:
                await Actor.charge(event_name=APK_EVENT)
                paid = True
            except Exception as exc:  # noqa: BLE001
                # Refuse the APK rather than the whole run: the generated source
                # is still worth returning, and it costs nothing to hand over.
                Actor.log.warning(
                    f"Could not charge for the APK ({exc}); generating source only."
                )
                paid = False

        payload = prd.model_dump(mode="json")
        # Set from the charge, never from the caller. A run that arrived with
        # this true in its own PRD would otherwise package an APK unpaid.
        payload["x402_payment_verified"] = paid

        if BUILD_DIR.exists():
            shutil.rmtree(BUILD_DIR)
        BUILD_DIR.mkdir(parents=True, exist_ok=True)

        generator = get_generator(generator_kind, **generator_kwargs)
        graph = build_graph(
            generator,
            get_analyzer("dart", os.getenv("FLUTTER_ROOT")),
            max_repairs=int(raw.get("maxRepairs", 3)),
            dry_run=not paid,
            flutter_root=os.getenv("FLUTTER_ROOT"),
            build_mode="debug",
            sdk_root=os.getenv("ANDROID_SDK_ROOT"),
        )

        Actor.log.info(f"Building {prd.app_name} ({generator_kind} generator)...")
        final = await asyncio.to_thread(
            graph.invoke, initial_state(payload, str(BUILD_DIR))
        )

        for line in final.get("log", []):
            Actor.log.info(line)

        diagnostics = [d.render() for d in final.get("diagnostics", [])]
        files = all_files(final)
        await Actor.push_data(_source_records(files))

        # The APK, if one was earned and produced.
        apk_url = None
        apk_path = final.get("apk_path")
        if paid and apk_path and Path(apk_path).is_file():
            store = await Actor.open_key_value_store()
            await store.set_value(
                "app.apk",
                Path(apk_path).read_bytes(),
                content_type="application/vnd.android.package-archive",
            )
            # Awaited: this is a coroutine, and forgetting so put the string
            # "<coroutine object ...>" in OUTPUT where the download link goes —
            # a buyer who paid would have had the APK sitting in storage and no
            # way to reach it.
            apk_url = await store.get_public_url("app.apk")
        elif want_apk and paid:
            Actor.log.warning("Packaging was paid for but produced no APK.")

        usage = getattr(generator, "usage", None)
        await Actor.set_value(
            "OUTPUT",
            {
                "app_name": prd.app_name,
                "generator": generator_kind,
                "files_generated": len(files),
                "diagnostics": diagnostics,
                "clean": not diagnostics and final.get("phase") != "failed",
                "apk": apk_url,
                "packaged": bool(apk_url),
                "usage": usage.public() if usage is not None else {},
            },
        )

        if final.get("phase") == "failed" or diagnostics:
            # Not Actor.fail(): the source is in the dataset and is worth having
            # even when the analyser rejected it. The diagnostics say why.
            await Actor.set_status_message(
                f"Generated with {len(diagnostics)} unresolved diagnostic(s) — "
                f"see OUTPUT. The source is in the dataset."
            )
        else:
            await Actor.set_status_message(
                f"{len(files)} files, analyser clean"
                + (", APK in the key-value store" if apk_url else "")
            )


if __name__ == "__main__":
    # Without this the module defines `main` and exits, `python -m` returns 0,
    # and Apify reports the run SUCCEEDED having built nothing at all — an empty
    # dataset behind a green tick, which is the worst way for this to fail.
    asyncio.run(main())
