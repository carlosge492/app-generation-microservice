"""Prove a release build is unsigned, and that a buyer can sign it.

    poetry run python scripts/verify_release_signing.py

Two claims, and the second is what makes the first useful rather than merely
austere:

  1. the release APK this service produces carries **no** signature — in
     particular not the Android debug key that the stock Flutter template wires
     into release builds
  2. a buyer holding their own keystore can sign that artifact and end up with
     one that verifies against their certificate

Claim 1 alone would be satisfied by a broken APK. Claim 2 is what shows the
artifact is a real, publishable app that is simply waiting for a key the service
never sees.

The keystore here is generated into a temporary directory and thrown away. It
stands in for the buyer's key; nothing in this repository ever holds a real one,
which is the whole design — see `src/build/signing.py`.

This runs a real `flutter build apk --release`, so it takes a few minutes.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.build.signing import find_apksigner, inspect_signature  # noqa: E402
from src.graph.builder import build_graph  # noqa: E402
from src.graph.state import initial_state  # noqa: E402
from src.ports.analyzer import get_analyzer  # noqa: E402
from src.ports.generator import get_generator  # noqa: E402
from src.prd.schema import load_prd  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PRD_PATH = ROOT / "examples" / "todo_app.prd.json"
STORE_PASS = "throwaway"
ALIAS = "buyer"


def log(message: str) -> None:
    print(f"  {message}", flush=True)


def find_keytool() -> str | None:
    found = shutil.which("keytool")
    if found:
        return found
    java_home = os.getenv("JAVA_HOME")
    if java_home:
        for name in ("keytool.exe", "keytool"):
            candidate = Path(java_home) / "bin" / name
            if candidate.exists():
                return str(candidate)
    # The JDK the Android build already relies on, wherever Flutter found it.
    for base in (Path(r"C:\Program Files\Microsoft"), Path(r"C:\Program Files\Java")):
        if not base.is_dir():
            continue
        for jdk in sorted(base.iterdir(), reverse=True):
            for name in ("keytool.exe", "keytool"):
                candidate = jdk / "bin" / name
                if candidate.exists():
                    return str(candidate)
    return None


def run(args: list[str], what: str) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        args, capture_output=True, text=True, check=False,
        encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        output = ((proc.stdout or "") + (proc.stderr or "")).strip()
        raise SystemExit(f"FAILED: {what} exited {proc.returncode}\n{output[-1500:]}")
    return proc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--flutter-root", default=os.getenv("FLUTTER_ROOT", r"C:\flutter"))
    parser.add_argument("--sdk-root", default=os.getenv("ANDROID_SDK_ROOT", r"C:\Android"))
    parser.add_argument("--build-dir", default=str(ROOT / "generated_apps" / "release_check"))
    args = parser.parse_args()

    print("\nRelease signing — unsigned by design, signable by the buyer\n" + "=" * 58)

    apksigner = find_apksigner(args.sdk_root)
    keytool = find_keytool()
    if apksigner is None:
        raise SystemExit(f"FAILED: no apksigner under {args.sdk_root}; cannot judge signatures")
    if keytool is None:
        raise SystemExit("FAILED: no keytool; cannot stand in for a buyer's keystore")
    log(f"apksigner: {apksigner}")
    log(f"keytool:   {keytool}")

    # -- 1. generate and package an app, in release mode -------------------- #
    build_dir = Path(args.build_dir)
    prd = load_prd(str(PRD_PATH))
    payload = prd.model_dump(mode="json")
    # The packaging gate, satisfied the way the server satisfies it. The script
    # is not a buyer and is not pretending to be: it drives the build path only,
    # and `tests/test_release_signing.py` proves the gate still refuses an
    # unpaid build in release mode.
    payload["x402_payment_verified"] = True

    log(f"generating and packaging {prd.app_name} in release mode (a few minutes)")
    graph = build_graph(
        get_generator("template"),
        get_analyzer("dart", args.flutter_root),
        max_repairs=3,
        test_runner=None,
        dry_run=False,
        flutter_root=args.flutter_root,
        build_mode="release",
        sdk_root=args.sdk_root,
    )
    final = graph.invoke(initial_state(payload, str(build_dir)))
    if final.get("phase") == "failed":
        raise SystemExit(f"FAILED: {final.get('failure') or 'the build failed'}")
    if not final.get("apk_path"):
        packaging = [line for line in final.get("log", []) if line.startswith("packaging:")]
        raise SystemExit(f"FAILED: no APK was produced. {' '.join(packaging)}")

    apk = Path(final["apk_path"])
    for line in final.get("log", []):
        if line.startswith("packaging:"):
            log(line)

    # -- 2. it must not be signed ------------------------------------------ #
    before = inspect_signature(apk, args.sdk_root)
    if not before.is_unsigned:
        raise SystemExit(
            f"FAILED: the release APK is {before.state}: {before.detail}"
            + (f" (signer: {before.signer})" if before.signer else "")
        )
    log(f"signature before: {before.state} — {before.detail}")

    # -- 3. a buyer signs it with their own key ---------------------------- #
    with tempfile.TemporaryDirectory() as tmp:
        keystore = Path(tmp) / "buyer.jks"
        signed = Path(tmp) / "signed.apk"
        dname = "CN=Buyer Verification, O=Throwaway, C=US"
        run(
            [
                keytool, "-genkeypair", "-keystore", str(keystore),
                "-storepass", STORE_PASS, "-keypass", STORE_PASS,
                "-alias", ALIAS, "-keyalg", "RSA", "-keysize", "2048",
                "-validity", "30", "-dname", dname,
            ],
            "keytool -genkeypair",
        )
        log("generated a throwaway keystore standing in for the buyer's")

        zipalign = Path(apksigner).parent / ("zipalign.exe" if os.name == "nt" else "zipalign")
        if zipalign.exists():
            run([str(zipalign), "-p", "-f", "4", str(apk), str(signed)], "zipalign")
        else:
            shutil.copy2(apk, signed)
        run(
            [
                apksigner, "sign", "--ks", str(keystore),
                "--ks-pass", f"pass:{STORE_PASS}", "--key-pass", f"pass:{STORE_PASS}",
                "--ks-key-alias", ALIAS, str(signed),
            ],
            "apksigner sign",
        )

        after = inspect_signature(signed, args.sdk_root)
        if after.state != "signed":
            raise SystemExit(f"FAILED: the signed APK does not verify: {after.detail}")
        if after.signer != dname:
            raise SystemExit(
                f"FAILED: signed by {after.signer!r}, expected the buyer's {dname!r}"
            )
        log(f"signature after:  {after.state} — {after.signer}")

    print("\n" + "=" * 58)
    print("PASSED: the release artifact is unsigned, and the buyer can sign it.")
    print(f"  apk        {apk}")
    print(f"  before     {before.state}")
    print(f"  after      signed by the buyer's own certificate")
    print("\nThe service never held that key, and never needs to.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
