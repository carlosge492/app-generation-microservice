"""The deployment files, checked against the code they configure.

None of this can prove the image builds — there is no container runtime on the
development machine, and the README says so. What it can prove is the class of
mistake that survives a successful `docker build` and shows up as a service that
runs and misbehaves: an environment variable spelled differently from the one
`src/service/app.py` reads, a toolchain version drifting away from the one the
verification table was proven on, or a `${VAR}` in compose that nothing supplies.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")
ENV_EXAMPLE = (ROOT / ".env.deploy.example").read_text(encoding="utf-8")
API_ENV = COMPOSE["services"]["api"]["environment"]


def test_the_toolchain_is_pinned_to_what_was_verified():
    """A floating Flutter or Android SDK turns a reproducible build environment
    into a moving one, and every compile claim in the README was made against
    these exact versions."""
    assert "FLUTTER_VERSION=3.44.8" in DOCKERFILE
    assert "platforms;android-36" in DOCKERFILE
    assert "build-tools;36.0.0" in DOCKERFILE
    assert "openjdk-17-jdk-headless" in DOCKERFILE


def test_the_image_sets_the_toolchain_paths_the_build_code_reads():
    """`_settings()` reads FLUTTER_ROOT and ANDROID_SDK_ROOT/ANDROID_HOME, and
    a build with either unset fails at packaging — after the money has moved."""
    for name in ("FLUTTER_ROOT", "ANDROID_SDK_ROOT", "ANDROID_HOME"):
        assert re.search(rf"^ENV .*{name}=|^\s+{name}=", DOCKERFILE, re.M), name


def test_compose_gives_the_service_a_redis():
    """Without REDIS_URL the job store is in memory: a restart loses builds that
    were paid for, and replay protection stops holding across processes."""
    assert API_ENV["REDIS_URL"].startswith("redis://")
    assert "redis" in COMPOSE["services"]
    assert COMPOSE["services"]["api"]["depends_on"]["redis"]["condition"] == "service_healthy"


def test_builds_are_written_to_a_volume():
    """On the overlay filesystem, a redeploy destroys every artifact a buyer has
    paid for and not yet downloaded."""
    api = COMPOSE["services"]["api"]
    mount = next(m for m in api["volumes"] if m.endswith(":/data/builds"))
    assert mount.split(":")[0] in COMPOSE["volumes"]
    assert api["environment"]["BUILD_ROOT"] == "/data/builds"


def test_every_substitution_in_compose_has_somewhere_to_come_from():
    """A `${VAR}` with no default and no line in the env template is an empty
    string at runtime. For X402_PAY_TO that means payments to nowhere."""
    referenced = {
        match.group(1)
        for match in re.finditer(r"\$\{([A-Z0-9_]+)\}", (ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    }
    documented = {
        line.split("=", 1)[0].strip()
        for line in ENV_EXAMPLE.splitlines()
        if "=" in line and not line.strip().startswith("#")
    }
    assert referenced <= documented, f"undocumented: {sorted(referenced - documented)}"


def test_the_env_template_ships_the_verified_testnet_parameters():
    """These are the values the end-to-end on-chain run was made with. A
    deployment that changes them silently is not the thing that was verified."""
    assert "X402_TOKEN_CONTRACT=0x036CbD53842c5426634e7929541eC2318f3dCF7e" in ENV_EXAMPLE
    assert "X402_CHAIN_ID=84532" in ENV_EXAMPLE
    assert "X402_NETWORK=base-sepolia" in ENV_EXAMPLE


def test_the_receiving_address_has_no_default():
    """Every other value can ship with a sensible default. This one cannot: a
    wrong address sends every buyer's payment somewhere the operator does not
    control, and it would be indistinguishable from working."""
    assert re.search(r"^X402_PAY_TO=\s*$", ENV_EXAMPLE, re.M)


def test_secrets_never_reach_the_image():
    """`.x402-testnet.json` holds a funded private key and `.env` the Anthropic
    key. Both are in the build context directory."""
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    for secret in (".env", ".x402-testnet.json"):
        assert secret in dockerignore
    assert ".env.deploy" in (ROOT / ".gitignore").read_text(encoding="utf-8")
