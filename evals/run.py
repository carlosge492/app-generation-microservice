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
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

from src.graph.builder import build_graph  # noqa: E402
from src.graph.state import initial_state  # noqa: E402
from src.ports.analyzer import get_analyzer, is_fatal  # noqa: E402
from src.ports.conformance import check_conformance  # noqa: E402
from src.ports.generator import get_generator
from src.ports.runtime import FlutterTestRunner  # noqa: E402
from src.ports.smoke import build_smoke_tests  # noqa: E402
from src.prd.schema import load_prd  # noqa: E402

PRD_DIRS = [Path("evals/prds"), Path("examples")]
# Per-generator, because they are not interchangeable. A template sweep run as
# a "free regression check" once overwrote a paid Claude sweep's output, and the
# regrade that followed silently graded the wrong code — reporting 10/11 for a
# model whose output no longer existed on disk. Sweeps must not clobber each
# other's evidence.
OUT_ROOT = Path("generated_apps") / "evals"


def out_root(generator: str) -> Path:
    return OUT_ROOT / generator


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


SOURCE_SUFFIXES = (".dart", ".yaml", ".md")


def _read_project(out_dir: Path) -> dict[str, str]:
    """Load a previously generated project back into the pipeline's file map."""
    files: dict[str, str] = {}
    for path in out_dir.rglob("*"):
        if not path.is_file() or path.suffix not in SOURCE_SUFFIXES:
            continue
        parts = path.relative_to(out_dir).parts
        if any(p in {".dart_tool", "build", "android", "ios", "test"} for p in parts):
            continue
        files["/".join(parts)] = path.read_text(encoding="utf-8")
    return files


def regrade_one(prd_path: Path, out_dir: Path, analyzer, runner) -> Result:
    """Grade output already on disk, without invoking a generator.

    Grading improved after these builds were produced, and regenerating them
    would cost another sweep's worth of API calls to re-test code that has not
    changed. This re-runs conformance, static analysis and the widget tests
    against what is already there.
    """
    name = prd_path.stem.replace(".prd", "")
    started = time.time()

    # Negative cases never produce output — they are supposed to be rejected by
    # the schema — so judge them the same way a real sweep does.
    if prd_path.name.startswith("invalid_"):
        try:
            load_prd(prd_path)
        except Exception as exc:
            first = str(exc).strip().splitlines()[0][:70]
            return Result(name, True, f"rejected as expected: {first}", 0.0)
        return Result(name, False, "expected validation to fail, but it passed", 0.0)

    if not (out_dir / "lib").exists():
        return Result(name, False, "no generated output on disk to regrade", 0.0)

    prd = load_prd(prd_path)
    files = _read_project(out_dir)

    # Smoke tests are regenerated from the current sources; stale ones from an
    # earlier generator would fail analysis for files that no longer exist.
    for old in (out_dir / "test").glob("*.dart"):
        old.unlink()
    for rel, body in build_smoke_tests(prd, files).items():
        target = out_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")

    diagnostics = list(check_conformance(prd, files))
    try:
        diagnostics += analyzer.analyze(out_dir)
        if runner is not None:
            diagnostics += runner.run(out_dir)
    except Exception as exc:
        return Result(name, False, f"{type(exc).__name__}: {str(exc)[:160]}",
                      time.time() - started)

    fatal = [d for d in diagnostics if is_fatal(d)]
    elapsed = time.time() - started
    if fatal:
        summary = "; ".join(d.render() for d in fatal[:3])
        if len(fatal) > 3:
            summary += f" (+{len(fatal) - 3} more)"
        return Result(name, False, f"{len(fatal)} unresolved: {summary}", elapsed)
    return Result(name, True, f"clean, {len(files)} source file(s)", elapsed)


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

    # The supervisor loads .env; this harness has to as well, or a
    # --generator claude sweep fails eleven times with an auth error.
    load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env")

    parser = argparse.ArgumentParser(prog="evals", description=__doc__)
    parser.add_argument("--analyzer", choices=["stub", "dart"], default="stub")
    parser.add_argument("--generator", choices=["template", "fixture", "claude"], default="template")
    parser.add_argument("--flutter-root", default=None)
    parser.add_argument("--max-repairs", type=int, default=3)
    parser.add_argument("--run-tests", action="store_true",
                        help="also run the generated widget smoke tests "
                             "(requires the Flutter SDK)")
    parser.add_argument("--regrade", action="store_true",
                        help="grade output already on disk; no generator, no API calls")
    parser.add_argument("--filter", default="", help="only run PRDs whose name contains this")
    args = parser.parse_args(argv)

    if args.generator == "claude" and not args.regrade and not os.getenv("ANTHROPIC_API_KEY"):
        print(
            "error: --generator claude needs ANTHROPIC_API_KEY (set it in .env). "
            "Failing now rather than once per PRD.",
            file=sys.stderr,
        )
        return 2

    prds = [p for p in collect_prds() if args.filter in p.stem]
    if not prds:
        print("no PRDs found", file=sys.stderr)
        return 2

    if args.regrade:
        analyzer = get_analyzer(args.analyzer, args.flutter_root)
        runner = FlutterTestRunner(args.flutter_root) if args.run_tests else None
        print(f"{len(prds)} PRD(s) | REGRADE of {out_root(args.generator)} "
              f"| analyzer={args.analyzer}")
        print("=" * 78)
        results = [
            regrade_one(p, out_root(args.generator) / p.stem.replace(".prd", ""),
                        analyzer, runner)
            for p in prds
        ]
        return _report(results)

    app = build_graph(
        get_generator(args.generator),
        get_analyzer(args.analyzer, args.flutter_root),
        max_repairs=args.max_repairs,
        test_runner=FlutterTestRunner(args.flutter_root) if args.run_tests else None,
        dry_run=True,
    )

    print(f"{len(prds)} PRD(s) | generator={args.generator} analyzer={args.analyzer} "
          f"| out={out_root(args.generator)}")
    print("=" * 78)

    results = [
        run_one(p, app, out_root(args.generator) / p.stem.replace(".prd", ""))
        for p in prds
    ]
    return _report(results)


def _report(results: list[Result]) -> int:
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
