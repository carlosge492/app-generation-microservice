"""Eval harness: run every PRD through the pipeline and report a pass rate.

    poetry run python evals/run.py                       # stub analyzer, seconds
    poetry run python evals/run.py --analyzer dart \
        --flutter-root "C:\\flutter"                     # real flutter analyze

"Zero-error compilation is non-negotiable" is only meaningful as a number. This
turns it into one.

A PRD whose filename starts with `invalid_` is a negative case: it is expected to
fail schema validation, and the harness fails if it does not.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.graph.builder import build_graph  # noqa: E402
from src.graph.state import initial_state  # noqa: E402
from src.ports.analyzer import get_analyzer  # noqa: E402
from src.ports.generator import get_generator
from src.ports.runtime import FlutterTestRunner  # noqa: E402
from src.prd.schema import load_prd  # noqa: E402

PRD_DIRS = [Path("evals/prds"), Path("examples")]
OUT_ROOT = Path("generated_apps") / "evals"


@dataclass
class Result:
    name: str
    ok: bool
    detail: str
    seconds: float
    repairs: int = 0

    @property
    def mark(self) -> str:
        return "PASS" if self.ok else "FAIL"


def collect_prds() -> list[Path]:
    found: list[Path] = []
    for directory in PRD_DIRS:
        found.extend(sorted(directory.glob("*.json")))
    return found


def run_one(path: Path, app, out_dir: Path) -> Result:
    name = path.stem.replace(".prd", "")
    negative = path.name.startswith("invalid_")
    started = time.time()

    try:
        prd = load_prd(path)
    except Exception as exc:
        elapsed = time.time() - started
        if negative:
            first = str(exc).strip().splitlines()[0][:70]
            return Result(name, True, f"rejected as expected: {first}", elapsed)
        return Result(name, False, f"PRD failed validation: {exc}", elapsed)

    if negative:
        return Result(name, False, "expected validation to fail, but it passed",
                      time.time() - started)

    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        final = app.invoke(initial_state(prd.model_dump(mode="json"), str(out_dir)))
    except Exception as exc:
        return Result(name, False, f"pipeline raised {type(exc).__name__}: {exc}",
                      time.time() - started)

    elapsed = time.time() - started
    repairs = final.get("repair_attempts", 0)

    if final.get("phase") == "failed":
        return Result(name, False, final.get("failure", "unknown"), elapsed, repairs)
    diagnostics = final.get("diagnostics", [])
    if diagnostics:
        summary = "; ".join(d.render() for d in diagnostics[:3])
        if len(diagnostics) > 3:
            summary += f" (+{len(diagnostics) - 3} more)"
        return Result(name, False, f"{len(diagnostics)} unresolved: {summary}",
                      elapsed, repairs)

    files = len(final.get("ui_files", {})) + len(final.get("provider_files", {}))
    return Result(name, True, f"clean, {files} generated file(s)", elapsed, repairs)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(prog="evals", description=__doc__)
    parser.add_argument("--analyzer", choices=["stub", "dart"], default="stub")
    parser.add_argument("--generator", choices=["template", "claude"], default="template")
    parser.add_argument("--flutter-root", default=None)
    parser.add_argument("--max-repairs", type=int, default=3)
    parser.add_argument("--run-tests", action="store_true",
                        help="also run the generated widget smoke tests "
                             "(requires the Flutter SDK)")
    parser.add_argument("--filter", default="", help="only run PRDs whose name contains this")
    args = parser.parse_args(argv)

    prds = [p for p in collect_prds() if args.filter in p.stem]
    if not prds:
        print("no PRDs found", file=sys.stderr)
        return 2

    app = build_graph(
        get_generator(args.generator),
        get_analyzer(args.analyzer, args.flutter_root),
        max_repairs=args.max_repairs,
        test_runner=FlutterTestRunner(args.flutter_root) if args.run_tests else None,
        dry_run=True,
    )

    print(f"{len(prds)} PRD(s) | generator={args.generator} analyzer={args.analyzer}")
    print("=" * 78)

    results = [run_one(p, app, OUT_ROOT / p.stem.replace(".prd", "")) for p in prds]

    width = max(len(r.name) for r in results)
    for r in results:
        repairs = f" r{r.repairs}" if r.repairs else "   "
        print(f"  {r.mark}  {r.name:<{width}}  {r.seconds:5.1f}s{repairs}  {r.detail}")

    passed = sum(1 for r in results if r.ok)
    total = len(results)
    print("=" * 78)
    print(f"{passed}/{total} passed  ({sum(r.seconds for r in results):.1f}s total)")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
