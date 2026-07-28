"""Orchestrator entry point.

    poetry run python src/supervisor.py examples/todo_app.prd.json

Defaults are offline: the template generator and the stub analyzer, so the whole
loop runs with no credentials and no Flutter SDK. Swap in the real ones with
`--generator claude` / `--analyzer dart`.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

# Allow both `python src/supervisor.py` (the documented command) and
# `python -m src.supervisor`.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

from src.graph.builder import build_graph  # noqa: E402
from src.graph.state import initial_state  # noqa: E402
from src.ports.analyzer import get_analyzer  # noqa: E402
from src.ports.generator import get_generator  # noqa: E402
from src.prd.schema import load_prd  # noqa: E402

DEFAULT_BUILD_DIR = Path("generated_apps") / "current_build"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="supervisor", description=__doc__)
    parser.add_argument("prd", nargs="?", default="examples/todo_app.prd.json",
                        help="path to the JSON PRD")
    parser.add_argument("--generator", choices=["template", "claude"], default="template",
                        help="template = offline; claude = real model calls")
    parser.add_argument("--analyzer", choices=["stub", "dart"], default="stub",
                        help="stub = no SDK needed; dart = real `dart analyze`")
    parser.add_argument("--build-dir", default=str(DEFAULT_BUILD_DIR))
    parser.add_argument("--flutter-root", default=os.getenv("FLUTTER_ROOT"),
                        help="Flutter SDK root; falls back to $FLUTTER_ROOT, then PATH")
    parser.add_argument("--max-repairs", type=int, default=3)
    parser.add_argument("--execute", action="store_true",
                        help="actually invoke fastlane (requires x402_payment_verified)")
    parser.add_argument("--clean", action="store_true",
                        help="wipe the build directory first")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    # Windows consoles default to cp1252 and mangle the log output.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    load_dotenv()
    args = parse_args(argv)

    try:
        prd = load_prd(args.prd)
    except FileNotFoundError:
        print(f"error: PRD not found: {args.prd}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"error: PRD failed validation:\n{exc}", file=sys.stderr)
        return 2

    build_dir = Path(args.build_dir)
    if args.clean and build_dir.exists():
        shutil.rmtree(build_dir)
    build_dir.mkdir(parents=True, exist_ok=True)

    try:
        generator = get_generator(args.generator)
    except Exception as exc:  # missing credentials, bad model id, ...
        print(f"error: could not construct the {args.generator!r} generator: {exc}",
              file=sys.stderr)
        if args.generator == "claude":
            print("hint: set ANTHROPIC_API_KEY, or rerun with --generator template.",
                  file=sys.stderr)
        return 2

    app = build_graph(
        generator,
        get_analyzer(args.analyzer, args.flutter_root),
        max_repairs=args.max_repairs,
        dry_run=not args.execute,
    )

    print(f"PRD      : {args.prd}  ({prd.app_name})")
    print(f"generator: {args.generator}    analyzer: {args.analyzer}"
          + (f"    flutter: {args.flutter_root}" if args.flutter_root else ""))
    print(f"build dir: {build_dir}")
    print("-" * 68)

    final = app.invoke(initial_state(prd.model_dump(mode="json"), str(build_dir)))

    for line in final.get("log", []):
        print(f"  {line}")
    print("-" * 68)

    diagnostics = final.get("diagnostics", [])
    if final.get("phase") == "failed":
        print(f"FAILED: {final.get('failure', 'unknown error')}", file=sys.stderr)
        return 1
    if diagnostics:
        print(f"FAILED: {len(diagnostics)} unresolved diagnostic(s) after "
              f"{final.get('repair_attempts', 0)} repair pass(es):", file=sys.stderr)
        for d in diagnostics:
            print(f"  {d.render()}", file=sys.stderr)
        return 1

    print(f"OK: zero-error build in {build_dir}")
    if not prd.x402_payment_verified:
        print("note: x402_payment_verified is false — APK packaging was gated off.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
