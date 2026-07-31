"""Prove a generated app actually reads and writes Firestore.

    poetry run python scripts/verify_firestore_roundtrip.py

Everything else the pipeline checks is static. `flutter analyze` proves the code
type-checks, conformance proves it matches the PRD, and the generated widget
tests prove the screens build. None of that executes a single Firestore call, so
"the generated app works" was never actually a claim this repo could make.

It was not a hypothetical gap. The first time this ran it found a bug in every
app the template generator had ever produced:

    TypeError: Instance of 'Timestamp':
    type 'Timestamp' is not a subtype of type 'DateTime?'

Firestore has no date type — a `DateTime` is stored as a `Timestamp` and comes
back as one — so a generated app threw the moment it read back a document it had
written itself. It type-checked. The widget tests passed, because they never put
a real document through the mapper.

What this does:

  1. starts the Firestore emulator (a `demo-*` project: no account, no keys,
     nothing that can touch a real project)
  2. generates the example app through the ordinary pipeline
  3. drops in an integration test that writes through the generated controller
     and reads back through the generated stream provider
  4. runs it in Chrome and checks every field survived the round trip

**On the platform.** This runs in Chrome, not on Android. `cloud_firestore` is
the same Dart code either way, so the mapper bug it catches is platform
independent — but "verified on web" is the honest claim, and the README says so.
Android would need an AVD and a system image, which this machine does not have.

**Requirements.** firebase-tools (npm), chromedriver matching the installed
Chrome, and a JDK 21+ for the emulator — note that the Android toolchain here
uses JDK 17, which firebase-tools refuses. Point `EMULATOR_JAVA_HOME` at a 21+
if the search below does not find one.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from src.graph.builder import build_graph  # noqa: E402
from src.graph.state import initial_state  # noqa: E402
from src.ports.analyzer import get_analyzer  # noqa: E402
from src.ports.generator import get_generator  # noqa: E402
from src.prd.schema import load_prd  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EMULATOR_DIR = ROOT / "emulator"
PRD_PATH = ROOT / "examples" / "todo_app.prd.json"
PROJECT_ID = "demo-appgen"
FIRESTORE_PORT = 8080
CHROMEDRIVER_PORT = 4444
WEB_PORT = 7365



def log(message: str) -> None:
    print(f"  {message}", flush=True)


def find_java21() -> str | None:
    """A JDK 21+, which is not the JDK the Android build uses.

    firebase-tools refuses anything older, and the toolchain here is pinned to
    17 for Gradle. Any 21+ runtime will do and it need not be on PATH — a JetBrains
    IDE's bundled runtime is one, which is what unblocked this machine.
    """
    candidates: list[Path] = []
    explicit = os.getenv("EMULATOR_JAVA_HOME")
    if explicit:
        candidates.append(Path(explicit))
    for base in (
        Path(r"C:\Program Files\Eclipse Adoptium"),
        Path(r"C:\Program Files\Microsoft"),
        Path(r"C:\Program Files\Java"),
        Path(r"C:\Program Files\JetBrains"),
    ):
        if base.is_dir():
            candidates.extend(sorted(base.iterdir(), reverse=True))

    for home in candidates:
        for java in (home / "bin" / "java.exe", home / "jbr" / "bin" / "java.exe",
                     home / "bin" / "java"):
            if not java.exists():
                continue
            proc = subprocess.run(
                [str(java), "-version"], capture_output=True, text=True,
                check=False, encoding="utf-8", errors="replace",
            )
            banner = (proc.stderr or "") + (proc.stdout or "")
            for token in banner.split('"'):
                major = token.split(".")[0]
                if major.isdigit() and int(major) >= 21:
                    return str(java.parent)
    return None


def assert_generation_succeeded(final: dict) -> None:
    """Stop unless the app really did analyse clean.

    `phase == "failed"` is not the only way to fail, and checking only it made
    both verification scripts claim "generated and analysed clean" for an app
    with 32 unresolved errors. When the repair budget runs out the router
    returns "fail", which is wired straight to END — it ends the graph without
    ever setting `phase`, so the last value written is whatever the QA node put
    there. The surviving diagnostics are right there in the state; nothing was
    reading them.

    Shared by both scripts because both had the identical hole.
    """
    if final.get("phase") == "failed":
        raise SystemExit(f"FAILED: generation failed: {final.get('failure')}")

    diagnostics = final.get("diagnostics") or []
    if diagnostics:
        rendered = "\n  ".join(d.render() for d in diagnostics[:15])
        more = f"\n  ... and {len(diagnostics) - 15} more" if len(diagnostics) > 15 else ""
        raise SystemExit(
            f"FAILED: generation finished with {len(diagnostics)} unresolved "
            f"diagnostic(s) — the repair budget ran out and the graph gave up:\n"
            f"  {rendered}{more}"
        )


def wait_until(predicate, timeout: float, description: str):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.5)
    raise SystemExit(f"FAILED: timed out after {timeout}s waiting for {description}")


def emulator_up() -> bool:
    try:
        return httpx.get(f"http://127.0.0.1:{FIRESTORE_PORT}/", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--flutter-root", default=os.getenv("FLUTTER_ROOT", r"C:\flutter"))
    parser.add_argument("--build-dir", default=str(ROOT / "generated_apps" / "firestore_check"))
    parser.add_argument("--keep-emulator", action="store_true",
                        help="assume a Firestore emulator is already listening")
    args = parser.parse_args()

    print("\nFirestore round trip — a generated app against a real emulator\n" + "=" * 61)

    firebase = shutil.which("firebase") or shutil.which("firebase.cmd")
    chromedriver = shutil.which("chromedriver") or shutil.which("chromedriver.cmd")
    if firebase is None:
        raise SystemExit("FAILED: firebase-tools not found (npm i -g firebase-tools)")
    if chromedriver is None:
        raise SystemExit("FAILED: chromedriver not found (npm i -g chromedriver@<chrome major>)")

    processes: list[subprocess.Popen] = []
    try:
        # -- emulator ------------------------------------------------------- #
        if args.keep_emulator or emulator_up():
            log(f"using the Firestore emulator already on :{FIRESTORE_PORT}")
        else:
            java_bin = find_java21()
            if java_bin is None:
                raise SystemExit(
                    "FAILED: firebase-tools needs a JDK 21+ and none was found. "
                    "The Android toolchain's JDK 17 will not do; set EMULATOR_JAVA_HOME."
                )
            log(f"emulator JDK: {java_bin}")
            env = dict(os.environ)
            env["PATH"] = java_bin + os.pathsep + env["PATH"]
            processes.append(subprocess.Popen(
                [firebase, "emulators:start", "--only", "firestore", "--project", PROJECT_ID],
                cwd=str(EMULATOR_DIR), env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            ))
            wait_until(emulator_up, 120, "the Firestore emulator")
            log(f"Firestore emulator listening on 127.0.0.1:{FIRESTORE_PORT}")

        processes.append(subprocess.Popen(
            [chromedriver, f"--port={CHROMEDRIVER_PORT}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ))
        log(f"chromedriver on :{CHROMEDRIVER_PORT}")

        # -- generate the app ----------------------------------------------- #
        build_dir = Path(args.build_dir)
        prd = load_prd(str(PRD_PATH))
        log(f"generating {prd.app_name} through the ordinary pipeline")
        graph = build_graph(
            get_generator("template"),
            get_analyzer("dart", args.flutter_root),
            max_repairs=3,
            test_runner=None,
            dry_run=True,  # no packaging: this is about runtime behaviour
            flutter_root=args.flutter_root,
        )
        final = graph.invoke(initial_state(prd.model_dump(mode="json"), str(build_dir)))
        assert_generation_succeeded(final)
        log("generated and analysed clean")

        # -- find the test the pipeline generated ---------------------------- #
        # Nothing is injected here any more. The QA phase emits the round-trip
        # test, its driver and the pubspec entry, so this script runs exactly
        # what a buyer receives. A copy kept in this file would drift from the
        # generated one and quietly start proving something else.
        targets = sorted((build_dir / "integration_test").glob("*_roundtrip_test.dart"))
        if not targets:
            raise SystemExit(
                "FAILED: the pipeline generated no round-trip test. Either the "
                "PRD declares no models, or the model's serialiser pair could "
                "not be found — see src/ports/roundtrip.py."
            )
        log(f"pipeline generated: {', '.join(t.name for t in targets)}")

        # -- run it --------------------------------------------------------- #
        flutter = str(Path(args.flutter_root) / "bin" / "flutter.bat")
        if not Path(flutter).exists():
            flutter = str(Path(args.flutter_root) / "bin" / "flutter")
        for target in targets:
            log(f"running {target.name} in Chrome (a couple of minutes)")
            proc = subprocess.run(
                [
                    flutter, "drive",
                    "--driver=test_driver/integration_test.dart",
                    f"--target=integration_test/{target.name}",
                    "-d", "chrome", "--browser-name=chrome",
                    f"--web-port={WEB_PORT}",
                    f"--dart-define=FIRESTORE_EMULATOR=127.0.0.1:{FIRESTORE_PORT}",
                ],
                cwd=str(build_dir), capture_output=True, text=True, check=False,
                encoding="utf-8", errors="replace",
            )
            output = (proc.stdout or "") + (proc.stderr or "")
            if proc.returncode != 0 or "All tests passed" not in output:
                interesting = [
                    line for line in output.splitlines()
                    if any(k in line for k in
                           ("TypeError", "Failure in", "Timeout", "Expected", "Actual"))
                ]
                raise SystemExit(
                    f"FAILED: {target.name} did not pass\n" + "\n".join(interesting[:15])
                )

        print("\n" + "=" * 61)
        print("PASSED: a generated app wrote to Firestore and read it back.")
        print(f"  ran       {len(targets)} generated round-trip test(s)")
        print("  write     through the app's own serialiser")
        print("  read      through the app's own deserialiser")
        print("  checked   text, number, bool and date survived the round trip")
        print("\nThe date is the one that used to throw.")
        return 0

    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=30)


if __name__ == "__main__":
    sys.exit(main())
