"""Prove a user can actually tap their way into every screen the PRD declares.

    poetry run python scripts/verify_navigation_flow.py

The static `unreachable_screen` check proves *something in the source navigates*
to each target. The generated navigation test's push cases prove the destination
*renders when pushed*. Neither proves the thing a buyer cares about: that a
person looking at the source screen can get there.

The gap between those claims is not hypothetical, and this script demonstrates it
rather than asserting it. It runs the generated navigation test twice:

  1. against the app the pipeline produced — everything green;
  2. against a mutant whose navigating affordance has been wrapped in
     `Offstage(offstage: true, ...)` — still in the file, still pointing at the
     right route, still compiling, and no longer on screen.

The mutant passes `unreachable_screen` (the `Navigator.pushNamed` call is right
there in the source) and passes the push case (the route table is untouched and
the destination renders). Exactly one test goes red: the tap case for the screen
that was mutated. That is the whole reason the tap cases exist, and running the
mutation is the only way to know they are not decorative.

**Requirements.** The same as `verify_firestore_roundtrip.py`, whose setup this
reuses: firebase-tools (npm), a JDK 21+ for the emulator, chromedriver matching
the browser, and `CHROME_EXECUTABLE` pointing at a Chrome-for-Testing binary —
`flutter drive` cannot get a controllable browser while ordinary Chrome is
running, and the failure looks like a code fault. See the README.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

# `--generator claude` needs ANTHROPIC_API_KEY, and the other entry points
# (src/supervisor.py, evals/run.py) already read it from .env. Doing the same
# here means the key is never passed on a command line, where it would end up in
# shell history and process listings.
load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env")

from verify_firestore_roundtrip import (  # noqa: E402
    FIRESTORE_PORT,
    PROJECT_ID,
    assert_generation_succeeded,
    emulator_up,
    find_java21,
    log,
    wait_until,
)

from src.graph.builder import build_graph  # noqa: E402
from src.graph.state import all_files, initial_state  # noqa: E402
from src.ports.analyzer import get_analyzer  # noqa: E402
from src.ports.conformance import check_conformance  # noqa: E402
from src.ports.generator import get_generator  # noqa: E402
from src.ports.templates import snake  # noqa: E402
from src.prd.schema import load_prd  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EMULATOR_DIR = ROOT / "emulator"
# Eight screens and three navigate actions across three different affordances:
# two list-screen floating action buttons and a settings-screen text button.
PRD_PATH = ROOT / "evals" / "prds" / "many_screens.prd.json"
# `flutter drive` looks for a WebDriver on 4444 unless told otherwise, and
# `--driver-port` is what tells it. Off the default so a round-trip run's
# chromedriver and this one cannot collide.
CHROMEDRIVER_PORT = 4445
WEB_PORT = 7366

# A healthy drive of this suite takes two to three minutes. `flutter drive`
# hangs here intermittently — observed with the web server listening, Chrome
# holding four established connections to it, chromedriver reporting a live
# session, and the tool's own process using 0.015 seconds of CPU across 23
# minutes — so the wait is bounded rather than left to run until someone
# notices. Generous enough that a slow machine is never mistaken for a hang.
DRIVE_TIMEOUT_SECONDS = 900

# The affordance a generator might have used. The mutation looks for the
# innermost one of these enclosing the navigation call, so it does not depend on
# which the generator picked — but it does have to recognise it, and says so
# loudly rather than silently mutating nothing.
AFFORDANCES = (
    "FloatingActionButton",
    "TextButton",
    "ElevatedButton",
    "OutlinedButton",
    "IconButton",
    "CupertinoButton",
    "ListTile",
    "InkWell",
    "GestureDetector",
)


def closing_paren(source: str, opening: int) -> int:
    depth = 0
    for index in range(opening, len(source)):
        if source[index] == "(":
            depth += 1
        elif source[index] == ")":
            depth -= 1
            if depth == 0:
                return index
    raise SystemExit("FAILED: unbalanced parentheses in generated Dart")


def hide_affordance(source: str, route: str) -> str:
    """Wrap the widget that navigates to `route` so it is no longer on screen.

    `Offstage` is the mutation of choice because of what it leaves alone. The
    widget stays in the file, so a grep-based check still sees it navigating.
    It stays in the widget tree, so nothing about the build breaks. It is simply
    never laid out — which is precisely the class of bug that makes an app ship
    with a screen no user can open.
    """
    call = f"Navigator.pushNamed(context, '{route}')"
    at = source.find(call)
    if at < 0:
        raise SystemExit(f"FAILED: no navigation to {route} to hide")

    innermost: tuple[int, int] | None = None
    for name in AFFORDANCES:
        for match in re.finditer(rf"\b{name}\(", source):
            opening = match.end() - 1
            closing = closing_paren(source, opening)
            if match.start() < at < closing:
                if innermost is None or match.start() > innermost[0]:
                    innermost = (match.start(), closing)
    if innermost is None:
        raise SystemExit(
            f"FAILED: the navigation to {route} is not inside any affordance "
            f"this script recognises, so there is nothing to hide. Add the "
            f"widget the generator used to AFFORDANCES."
        )

    start, end = innermost
    return (
        source[:start]
        + "Offstage(offstage: true, child: "
        + source[start:end + 1]
        + ")"
        + source[end + 1:]
    )


def port_is_busy(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(1)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def preflight_ports() -> None:
    """Refuse to start on top of a previous run's leftovers.

    Worth the twenty lines: a chromedriver left over from an earlier drive makes
    the next one hang at "Waiting for connection from debug service" with no
    output and no timeout, which cost two twenty-minute waits to recognise.
    Killing them is not this script's business — saying so is.
    """
    for port, what in ((CHROMEDRIVER_PORT, "chromedriver"), (WEB_PORT, "a web server")):
        if port_is_busy(port):
            raise SystemExit(
                f"FAILED: {what} is already listening on :{port}, probably left "
                f"behind by an earlier run. `flutter drive` will hang rather "
                f"than fail against it. Stop the stale chromedriver and any "
                f"Chrome-for-Testing processes, then run this again."
            )


def wipe_emulator() -> None:
    """Start from an empty database.

    Not hygiene for its own sake. The list screens' navigating affordance is a
    floating action button, which the Scaffold builds *after* the body — so
    every row left over from a previous run is an earlier candidate for the tap
    search than the button actually being looked for. Leave enough of them
    behind and a working app fails its own test on the tap budget.
    """
    httpx.delete(
        f"http://127.0.0.1:{FIRESTORE_PORT}/emulator/v1/projects/{PROJECT_ID}"
        f"/databases/(default)/documents",
        timeout=30,
    ).raise_for_status()


def failing_tests(output: str) -> set[str]:
    """Which tests failed, from either shape the run reports them in.

    `integrationDriver` collects every failure and prints a `Failure Details:`
    section at the end, one `Failure in method: <name>` per failure — that
    section is the reliable one, because it is written after all the tests have
    run rather than as they go. The `[E]` progress lines are package:test's own
    reporter and show up when a test blows up early; both are parsed because a
    verification script that reads only one of them reports "nothing
    identifiable" for half the ways a run can fail, which is how the first
    version of this wasted a ten-minute run.
    """
    names = set()
    for line in output.replace("\r", "\n").splitlines():
        summary = re.match(r"\s*Failure in method:\s*(.+?)\s*$", line)
        if summary:
            names.add(summary.group(1))
            continue
        progress = re.search(r"[+-]\d+.*?:\s(.*?)\s\[E\]", line)
        if progress:
            names.add(progress.group(1).strip())
    return names


def drive(flutter: str, build_dir: Path, target: str, log_to: Path) -> tuple[bool, str]:
    """Run one integration test in Chrome, keeping the whole output as evidence.

    The identifiers matter. The navigation test boots the app through its own
    `main()`, and that `main()` takes its Firebase configuration from
    `--dart-define`; with none supplied it passes `options: null`, which fails an
    assertion inside `firebase_core_web` before the first frame and every test
    fails for the same uninteresting reason. They are `demo-` values against an
    emulator, so nothing here can reach a real project.
    """
    command = [
        flutter, "drive",
        "--driver=test_driver/integration_test.dart",
        f"--target=integration_test/{target}",
        "-d", "chrome", "--browser-name=chrome",
        f"--driver-port={CHROMEDRIVER_PORT}",
        f"--web-port={WEB_PORT}",
        f"--dart-define=FIRESTORE_EMULATOR=127.0.0.1:{FIRESTORE_PORT}",
        f"--dart-define=FIREBASE_PROJECT_ID={PROJECT_ID}",
        "--dart-define=FIREBASE_API_KEY=demo-key",
        "--dart-define=FIREBASE_APP_ID=1:1:web:demo",
        "--dart-define=FIREBASE_MESSAGING_SENDER_ID=1",
    ]

    # Streamed to the log as it arrives, rather than captured and written at the
    # end. `flutter drive` hangs here occasionally — the browser connects, the
    # web server serves, and then nothing finishes — and with captured output
    # there is no way to see where it stopped, because the output only lands
    # when the process exits, which is precisely what is not happening. Watch it
    # with `tail -f` on the path this returns.
    with open(log_to, "w", encoding="utf-8", errors="replace") as sink:
        proc = subprocess.Popen(
            command, cwd=str(build_dir), stdout=sink,
            stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace",
        )
        try:
            proc.wait(timeout=DRIVE_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            # An unbounded wait turns a hung browser into a hung script, which
            # is how this cost two twenty-minute waits before anyone recognised
            # it. Killing it leaves a log that ends where the hang began.
            proc.kill()
            proc.wait(timeout=30)
            sink.write(
                f"\n\n*** verify_navigation_flow.py killed `flutter drive` after "
                f"{DRIVE_TIMEOUT_SECONDS}s: it had not finished. ***\n"
            )

    output = log_to.read_text(encoding="utf-8", errors="replace")
    return proc.returncode == 0 and "All tests passed" in output, output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--flutter-root", default=os.getenv("FLUTTER_ROOT", r"C:\flutter"))
    parser.add_argument("--build-dir", default=str(ROOT / "generated_apps" / "nav_flow_check"))
    parser.add_argument("--keep-emulator", action="store_true",
                        help="assume a Firestore emulator is already listening")
    parser.add_argument("--generator", default="template",
                        help="whose widget choices to run against; the whole "
                             "point of the affordance search is that this "
                             "should not matter, which is worth checking")
    args = parser.parse_args()

    print("\nNavigation — can a user actually get there?\n" + "=" * 61)

    firebase = shutil.which("firebase") or shutil.which("firebase.cmd")
    chromedriver = shutil.which("chromedriver") or shutil.which("chromedriver.cmd")
    if firebase is None:
        raise SystemExit("FAILED: firebase-tools not found (npm i -g firebase-tools)")
    if chromedriver is None:
        raise SystemExit("FAILED: chromedriver not found (npm i -g chromedriver@<chrome major>)")

    preflight_ports()

    processes: list[subprocess.Popen] = []
    mutated: tuple[Path, str] | None = None
    try:
        # -- emulator, so list screens settle instead of spinning ------------- #
        if args.keep_emulator or emulator_up():
            log(f"using the Firestore emulator already on :{FIRESTORE_PORT}")
        else:
            java_bin = find_java21()
            if java_bin is None:
                raise SystemExit(
                    "FAILED: firebase-tools needs a JDK 21+ and none was found. "
                    "The Android toolchain's JDK 17 will not do; set EMULATOR_JAVA_HOME."
                )
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

        # -- generate the app through the ordinary pipeline ------------------- #
        build_dir = Path(args.build_dir)
        prd = load_prd(str(PRD_PATH))
        log(f"generating {prd.app_name} — {len(prd.screens)} screens, "
            f"{args.generator} generator")
        graph = build_graph(
            get_generator(args.generator),
            get_analyzer("dart", args.flutter_root),
            max_repairs=3,
            test_runner=None,
            dry_run=True,  # no packaging: this is about runtime behaviour
            flutter_root=args.flutter_root,
        )
        final = graph.invoke(initial_state(prd.model_dump(mode="json"), str(build_dir)))
        assert_generation_succeeded(final)
        log("generated and analysed clean")

        target = "navigation_test.dart"
        if not (build_dir / "integration_test" / target).exists():
            raise SystemExit(
                "FAILED: the pipeline generated no navigation test — see "
                "src/ports/navigation.py"
            )

        flutter = str(Path(args.flutter_root) / "bin" / "flutter.bat")
        if not Path(flutter).exists():
            flutter = str(Path(args.flutter_root) / "bin" / "flutter")

        # -- 1. the app as generated ------------------------------------------ #
        wipe_emulator()
        log("running the generated navigation test in Chrome (a few minutes)")
        clean_log = build_dir / "navigation_as_generated.log"
        passed, output = drive(flutter, build_dir, target, clean_log)
        if not passed:
            raise SystemExit(
                "FAILED: the generated app does not pass its own navigation test\n  "
                + "\n  ".join(sorted(failing_tests(output)) or output.splitlines()[-8:])
                + f"\n  full output: {clean_log}"
            )
        log("green on the unmodified app")

        # -- 2. the same app with one affordance taken off screen ------------- #
        mutation = next(
            (screen, action)
            for screen in prd.screens
            for action in screen.actions
            if action.kind == "navigate" and action.target
        )
        screen, action = mutation
        screen_file = build_dir / "lib" / "ui" / f"{snake(screen.id)}_screen.dart"
        if not screen_file.exists():
            raise SystemExit(f"FAILED: no source file for screen {screen.id!r}")
        original = screen_file.read_text(encoding="utf-8")
        mutated = (screen_file, original)
        screen_file.write_text(
            hide_affordance(original, f"/{action.target}"), encoding="utf-8"
        )
        log(f"hid the affordance on {screen.id} that navigates to {action.target}")

        # The mutant is only interesting if the static checks still pass on it.
        # Asserting that in a print statement would be a claim; running the
        # check is evidence, and it costs nothing.
        mutant_files = all_files(final)
        mutant_files[f"lib/ui/{snake(screen.id)}_screen.dart"] = (
            screen_file.read_text(encoding="utf-8")
        )
        survivors = [
            diagnostic for diagnostic in check_conformance(prd, mutant_files)
            if diagnostic.code == "unreachable_screen"
        ]
        if survivors:
            raise SystemExit(
                "FAILED: the mutation was supposed to leave the static checks "
                "green, so that the tap case is demonstrably catching something "
                "they cannot. unreachable_screen fired:\n  "
                + "\n  ".join(d.message for d in survivors)
            )
        log("conformance still passes on the mutant — the call is right there")
        wipe_emulator()

        expected = f"{action.name}: a tap on {screen.id} reaches {action.target}"
        mutant_log = build_dir / "navigation_with_affordance_hidden.log"
        passed, output = drive(flutter, build_dir, target, mutant_log)
        failed = failing_tests(output)
        if passed:
            raise SystemExit(
                "FAILED: the navigation test passed against an app whose only way "
                f"into {action.target} is off screen. The tap cases are not "
                "testing anything."
            )
        if expected not in failed:
            raise SystemExit(
                "FAILED: the mutant failed, but not on the test that should have "
                f"caught it.\n  expected: {expected}\n  actually failed: "
                + (", ".join(sorted(failed)) or "nothing identifiable")
            )
        if len(failed) != 1:
            raise SystemExit(
                "FAILED: hiding one affordance broke more than one test, so the "
                "failure does not isolate the claim:\n  " + "\n  ".join(sorted(failed))
            )

        print("\n" + "=" * 61)
        print("PASSED: the tap cases catch a screen nobody can reach.")
        print(f"  green     the generated app, all {len(prd.screens)} screens")
        print(f"  mutation  hid the {screen.id} affordance to {action.target}")
        print("  survived  unreachable_screen, run against the mutant")
        print("  survived  the push case (the route still renders the screen)")
        print(f"  caught by {expected}")
        print(f"\n  evidence  {clean_log}\n            {mutant_log}")
        return 0

    finally:
        if mutated is not None:
            mutated[0].write_text(mutated[1], encoding="utf-8")
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=30)


if __name__ == "__main__":
    sys.exit(main())
