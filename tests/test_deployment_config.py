"""The deployment files, checked against the code they configure.

None of this can prove the image builds — there is no container runtime on the
development machine, and the README says so. What it can prove is the class of
mistake that survives a successful `docker build` and shows up as a service that
runs and misbehaves: an environment variable spelled differently from the one
`src/service/app.py` reads, a toolchain version drifting away from the one the
verification table was proven on, or a `${VAR}` in compose that nothing supplies.
"""

from __future__ import annotations

import ast
import io
import json
import re
import subprocess
import sys
import tarfile
from fnmatch import fnmatch
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")
# The instructions alone. Several comments name the mistakes they exist to warn
# against, so a check for "this string is absent" has to read past them.
INSTRUCTIONS = "\n".join(
    line for line in DOCKERFILE.splitlines() if not line.lstrip().startswith("#")
)
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


def test_root_work_happens_before_the_user_switch():
    """Three failures that only a real `docker build` would otherwise find, and
    this machine has no container runtime to find them with.

    After `USER builder`, a RUN cannot create `/data` (root owns `/`) and cannot
    write to the system site-packages. Both fail the build, and neither error
    names the ordering as the cause — the site-packages one in particular reads
    like a broken dependency.
    """
    switch = DOCKERFILE.index("USER builder")

    assert DOCKERFILE.index("mkdir -p /data/builds") < switch, \
        "/data/builds must be created while still root"
    assert DOCKERFILE.index("poetry install --only main") < switch, \
        "dependencies must be installed system-wide as root"
    assert DOCKERFILE.index("chown -R builder:builder") < switch


def test_the_entrypoint_binary_is_on_path_for_the_build_user():
    """`pip install --user` would put uvicorn in ~/.local/bin, which this
    image's PATH does not include — the container would build clean and then
    fail to start with 'executable file not found'."""
    assert "pip install --user" not in INSTRUCTIONS
    assert "command -v uvicorn" in INSTRUCTIONS


def test_the_image_refuses_to_build_on_the_wrong_architecture():
    """The Flutter Linux SDK is x64-only and the Android build-tools are x86_64
    ELF, so an arm64 host — the cheap default at most providers — fails deep
    inside Gradle with an exec-format error that names none of this."""
    assert "dpkg --print-architecture" in DOCKERFILE
    assert DOCKERFILE.index("print-architecture") < DOCKERFILE.index("flutter_linux_")


def test_the_android_toolchain_is_asserted_not_assumed():
    """`flutter doctor` exits 0 with pieces of the toolchain missing. apksigner
    is what src/build/signing.py shells out to, and an image without it packages
    an APK and then fails at the last step, after payment."""
    assert "build-tools/36.0.0/apksigner" in DOCKERFILE
    assert "platforms/android-36" in DOCKERFILE


def test_compose_gives_the_service_a_redis():
    """Without REDIS_URL the job store is in memory: a restart loses builds that
    were paid for, and replay protection stops holding across processes."""
    assert API_ENV["REDIS_URL"].startswith("redis://")
    assert "redis" in COMPOSE["services"]
    assert COMPOSE["services"]["api"]["depends_on"]["redis"]["condition"] == "service_healthy"


def test_the_api_port_is_not_published_to_the_world():
    """Docker's published ports are nat rules that sit ahead of ufw, so a
    `0.0.0.0` binding is reachable from the internet no matter what the firewall
    says. Over plain HTTP that puts signed payment authorizations and the
    buyer's PRD in the clear."""
    for mapping in COMPOSE["services"]["api"]["ports"]:
        assert str(mapping).startswith("127.0.0.1:"), (
            f"{mapping} publishes beyond loopback; put a TLS proxy in front "
            f"instead, which reaches the service over the compose network"
        )


def test_tls_terminates_in_front_and_the_api_stays_private():
    """Only the proxy is on a public port. The API is reached over the compose
    network, so the X-PAYMENT header and the buyer's PRD never cross the network
    unencrypted."""
    caddy = COMPOSE["services"]["caddy"]
    published = {str(p).split(":")[0] for p in caddy["ports"]}

    assert {"80", "443"} <= published
    assert "8000" not in published, "the API is not the proxy's job to expose"
    assert "api" in caddy["depends_on"]


def test_certificates_survive_a_redeploy():
    """Let's Encrypt allows five certificates per week for the same name. Losing
    /data on every `up --build` would exhaust that in a day and leave the
    service publicly unreachable until the window rolled over."""
    caddy = COMPOSE["services"]["caddy"]
    data_mount = next(m for m in caddy["volumes"] if m.endswith(":/data"))

    assert data_mount.split(":")[0] in COMPOSE["volumes"]


def test_the_proxy_does_not_cut_off_a_build_or_a_download():
    """A build runs for minutes and the APK is ~150 MB. Caddy's default proxy
    timeouts would give a buyer a gateway error mid-build and a truncated
    download."""
    caddyfile = (ROOT / "deploy" / "Caddyfile").read_text(encoding="utf-8")

    assert "read_timeout 30m" in caddyfile
    assert "write_timeout 30m" in caddyfile


def test_no_caddy_directive_can_expand_to_nothing():
    """`email {$ACME_EMAIL:}` is not "email, defaulting to none" — it expands to
    a bare `email` with no argument, which is a parse error. Caddy then
    restart-loops and never binds 443, and from outside that is indistinguishable
    from a closed firewall port. Any directive whose only argument is an
    empty-defaulted substitution has the same shape."""
    caddyfile = (ROOT / "deploy" / "Caddyfile").read_text(encoding="utf-8")

    offenders = [
        line.strip()
        for line in caddyfile.splitlines()
        if not line.strip().startswith("#")
        and re.search(r"^\s*\w+\s+\{\$[A-Z_]+:\}\s*$", line)
    ]
    assert not offenders, f"expands to a bare directive when unset: {offenders}"


def test_the_acme_challenge_matches_the_open_ports():
    """Port 80 is closed at the cloud firewall, so an HTTP-01 challenge cannot
    complete. Leaving it enabled costs a failed attempt before the fallback."""
    caddyfile = (ROOT / "deploy" / "Caddyfile").read_text(encoding="utf-8")

    assert "disable_http_challenge" in caddyfile


def test_the_mainnet_domain_name_is_documented_where_it_is_needed():
    """`X402_DOMAIN_NAME` defaults to "USDC", which is correct for Base Sepolia
    and wrong for Base mainnet, where USDC's EIP-712 domain name is "USD Coin".
    A deployment that changes the chain and contract but not the domain rejects
    every buyer signature. The example file is where someone reads the mainnet
    values, so it is where that has to be said."""
    assert "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913" in ENV_EXAMPLE
    assert "X402_DOMAIN_NAME=USD Coin" in ENV_EXAMPLE
    assert "X402_CHAIN_ID=8453" in ENV_EXAMPLE


def test_the_settings_that_decide_cost_and_output_reach_the_container():
    """A variable the compose file does not pass simply is not there, and the
    code falls back to its own default without saying so. For SUPERVISOR_MODEL
    that means a deployment pinning a cheap model keeps billing at the default
    model's rate, silently — the failure has no symptom to notice."""
    for name in ("SUPERVISOR_GENERATOR", "SUPERVISOR_MODEL", "SUPERVISOR_BUILD_MODE"):
        assert name in API_ENV, f"{name} never reaches the container"


APP_SOURCE = (ROOT / "src" / "service" / "app.py").read_text(encoding="utf-8")
# The dev stand-in's secret, deliberately absent from compose: passing it would
# let a production deployment fall back to the verifier that accepts unsigned
# promises, which is the one thing the payment gate exists to prevent.
NOT_FOR_THE_CONTAINER = {"X402_SHARED_SECRET"}


def test_every_payment_setting_the_service_reads_reaches_the_container():
    """Generalises the two variables that have already been lost this way.

    A name the compose file does not list is simply not in the container, and
    the code falls back to its own default in silence. That cost a mainnet
    deployment its EIP-712 domain — the service ran, looked healthy, and would
    have rejected every buyer signature. Enumerating the reads instead of the
    known-bad names is the point: a variable added later is covered on the day
    it is added, not after it has been debugged from the wrong end.
    """
    read = {
        match.group(1)
        for match in re.finditer(r'_?env\(\s*"(X402_[A-Z0-9_]+)"', APP_SOURCE)
    }
    assert read, "no X402 settings found; this test is matching nothing"

    missing = read - set(API_ENV) - NOT_FOR_THE_CONTAINER
    assert not missing, f"read by the service but never passed to it: {sorted(missing)}"


def test_optional_settings_survive_being_passed_as_an_empty_string():
    """The other half of the same mistake, and the more damaging half.

    `${NAME:-}` does not mean "omit when unset" — it puts an empty string in the
    container. `os.getenv(name, default)` falls back only on *absence*, so the
    empty string wins: `int("")` raises before the service can serve anything,
    and an empty domain name rejects every signature with no default to catch
    it. Both are strictly worse than the unset case the `:-` was added for.
    """
    optional = {
        match.group(1)
        for match in re.finditer(r"\$\{(X402_[A-Z0-9_]+):-\}", (ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    }
    assert optional, "no empty-defaulted X402 settings found; this test is matching nothing"

    unsafe = [
        name for name in optional
        if re.search(rf'os\.getenv\(\s*"{name}"', APP_SOURCE)
    ]
    assert not unsafe, (
        f"passed as '' but read with os.getenv's positional default, which only "
        f"applies when the name is absent: {sorted(unsafe)}"
    )


def test_an_unset_model_is_not_passed_as_an_empty_string():
    """`${SUPERVISOR_MODEL:-}` puts an empty string in the container, and
    `os.getenv(name, default)` falls back only on absence — so the generator
    has to treat empty as unset or it sends "" to the API as a model name."""
    llm = (ROOT / "src" / "ports" / "llm.py").read_text(encoding="utf-8")

    assert 'os.getenv("SUPERVISOR_MODEL") or model' in llm


def test_container_logs_are_bounded():
    """The default json-file driver grows without limit, on a host already
    sized around 1.9 GB of build artifact apiece."""
    options = COMPOSE["services"]["api"]["logging"]["options"]
    assert options["max-size"] and options["max-file"]


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


ACTOR_MAIN = (ROOT / "src" / "actor" / "main.py").read_text(encoding="utf-8")
ACTOR_DOCKERFILE = (ROOT / "Dockerfile.actor").read_text(encoding="utf-8")


def test_the_two_images_pin_the_same_toolchain():
    """The Actor image duplicates the toolchain, so it can drift from the
    service's.

    It has to be self-contained: Apify builds it on its own runners and cannot
    pull a base image that exists only on our machine. The cost of that is two
    copies of the versions every compile claim in the README was proven
    against. Two images on different SDKs would let the Actor and the hosted
    service promise the same guarantee while building differently — and the
    difference would surface as a buyer's APK failing, not as a failed build.
    """
    pinned = (
        "FLUTTER_VERSION=3.44.8",
        "platforms;android-36",
        "build-tools;36.0.0",
        "openjdk-17-jdk-headless",
        "commandlinetools-linux-13114758_latest.zip",
    )
    for version in pinned:
        assert version in DOCKERFILE, f"{version} vanished from Dockerfile"
        assert version in ACTOR_DOCKERFILE, (
            f"{version} is pinned in Dockerfile but not Dockerfile.actor; "
            f"the Actor would build against a different toolchain"
        )

    # The assertions that catch a half-installed SDK, which `flutter doctor`
    # does not: both images must keep them.
    for guarded in ("build-tools/36.0.0/apksigner", "platforms/android-36"):
        assert guarded in ACTOR_DOCKERFILE, f"the Actor image stopped asserting {guarded}"


def test_the_actor_image_is_self_contained():
    """`FROM appgen-base` builds locally and fails on Apify, which has no such
    image — a mistake that only shows up at publish time, on their runners."""
    first_from = next(
        line for line in ACTOR_DOCKERFILE.splitlines()
        if line.strip().startswith("FROM")
    )
    assert "appgen" not in first_from.lower(), (
        f"the Actor image depends on a local base Apify cannot pull: {first_from}"
    )
    assert first_from.split()[1].startswith("python:"), first_from


def test_the_actor_earns_the_packaging_flag_rather_than_asserting_it():
    """CLAUDE.md §4 on a different payment rail.

    Apify replaces the x402 settlement, so the Actor is the one place where
    `x402_payment_verified` could plausibly be set to a literal True and nobody
    would notice until APKs were going out unpaid. It has to come from a charge
    that succeeded, and the charge has to happen before the graph runs — after
    would be a build already given away.
    """
    tree = ast.parse(ACTOR_MAIN)

    literal_true = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Subscript)
            and isinstance(target.slice, ast.Constant)
            and target.slice.value == "x402_payment_verified"
            for target in node.targets
        )
        and isinstance(node.value, ast.Constant)
    ]
    assert not literal_true, "the packaging flag is hardcoded rather than earned"

    assert 'payload["x402_payment_verified"] = paid' in ACTOR_MAIN
    # The charge decides `paid`, and does so before the pipeline is invoked.
    assert ACTOR_MAIN.index("Actor.charge") < ACTOR_MAIN.index("graph.invoke")


def test_the_actor_does_not_trust_the_callers_own_payment_flag():
    """The PRD is caller-supplied on this path too. A run arriving with the flag
    already true must not package anything — same discard the hosted service
    does in `_verified_prd`/`_preview_prd`."""
    tree = ast.parse(ACTOR_MAIN)
    assignments = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(t, ast.Subscript)
            and isinstance(t.slice, ast.Constant)
            and t.slice.value == "x402_payment_verified"
            for t in node.targets
        )
    ]
    assert len(assignments) == 1, "the flag is set in more than one place"
    assert isinstance(assignments[0].value, ast.Name), \
        "the flag must come from a variable the charge set, not from the input"


def test_the_output_schema_points_at_things_the_actor_writes():
    """The Store's publication checklist requires an output schema, and an
    incomplete checklist leaves the Actor unlisted — `isPublic` goes true while
    it stays absent from search and category browse, reachable only by direct
    link. That is how this one shipped invisible for twelve days.

    Pointing the schema at a key nothing writes would satisfy the checklist and
    still hand users a dead link, so each template is checked against the code
    that produces it.
    """
    actor = json.loads((ROOT / ".actor" / "actor.json").read_text(encoding="utf-8"))
    assert actor.get("output"), "actor.json does not declare an output schema"

    schema = json.loads(
        (ROOT / ".actor" / actor["output"].lstrip("./")).read_text(encoding="utf-8")
    )
    for field in ("actorOutputSchemaVersion", "title", "properties"):
        assert field in schema, f"output schema is missing {field}"

    main = (ROOT / "src" / "actor" / "main.py").read_text(encoding="utf-8")
    for name, prop in schema["properties"].items():
        # `type` is required by Apify's validator and absent from the example in
        # their docs, so the first version of this schema was rejected at build
        # time — 0-second failure, before the image is even attempted. Asserted
        # here because a test that only encodes what the docs showed passes
        # while the platform refuses the build.
        assert prop.get("type"), f"{name} has no 'type'; Apify's validator requires it"
        assert prop.get("title") and prop.get("template"), name
        template = prop["template"]
        # A key-value record referenced here has to be one main.py stores.
        if "/records/" in template:
            key = template.rsplit("/records/", 1)[1]
            assert f'"{key}"' in main, (
                f"output schema offers {key!r} but nothing in main.py writes it"
            )
        # A dataset view referenced here has to exist in actor.json.
        if "view=" in template:
            view = template.rsplit("view=", 1)[1]
            views = actor.get("storages", {}).get("dataset", {}).get("views", {})
            assert view in views, f"template names view {view!r}, declared: {list(views)}"


def test_the_key_value_store_schema_describes_keys_that_exist():
    """Optional on the publication checklist, but only worth adding if true.

    It advertises what a run leaves behind, so a collection naming a key the
    Actor never writes is a promise broken in the console rather than a missing
    tick — worse than leaving the schema out. The live-view OpenAPI schema is
    deliberately *not* provided: it describes an Actor serving HTTP from a
    persistent web server, and this one runs once and exits.
    """
    actor = json.loads((ROOT / ".actor" / "actor.json").read_text(encoding="utf-8"))
    reference = actor.get("storages", {}).get("keyValueStore")
    assert reference, "actor.json does not reference a key-value store schema"

    schema = json.loads(
        (ROOT / ".actor" / str(reference).lstrip("./")).read_text(encoding="utf-8")
    )
    assert schema.get("actorKeyValueStoreSchemaVersion") == 1
    assert schema.get("title") and schema.get("collections")

    main = (ROOT / "src" / "actor" / "main.py").read_text(encoding="utf-8")
    for name, collection in schema["collections"].items():
        assert collection.get("title"), name
        # Exactly one of key/keyPrefix, per the spec.
        assert bool(collection.get("key")) != bool(collection.get("keyPrefix")), (
            f"{name} must set exactly one of key or keyPrefix"
        )
        if key := collection.get("key"):
            assert f'"{key}"' in main, (
                f"the schema advertises {key!r} but nothing in main.py writes it"
            )


def test_every_async_apify_call_is_awaited():
    """An un-awaited coroutine is silent in Python, and this one shipped.

    `store.get_public_url(...)` without `await` put the literal string
    "<coroutine object KeyValueStore.get_public_url at 0x...>" into OUTPUT where
    the APK download link belongs. The run succeeded, the APK was really built
    and really stored — and a buyer who paid for it would have had no way to
    reach it. Nothing raised, nothing logged, and the status said SUCCEEDED.
    """
    tree = ast.parse(ACTOR_MAIN)

    # Apify SDK methods used here that return coroutines.
    async_methods = {
        "get_input", "push_data", "set_value", "open_key_value_store",
        "get_public_url", "charge", "set_status_message", "fail",
    }

    awaited = {
        id(node.value) for node in ast.walk(tree)
        if isinstance(node, ast.Await) and isinstance(node.value, ast.Call)
    }
    bare = [
        ast.unparse(node) for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in async_methods
        and id(node) not in awaited
    ]
    assert not bare, f"async Apify calls used without await: {bare}"


def test_running_the_actor_module_actually_runs_it():
    """`python -m src.actor.main` must do something.

    The first version defined `async def main()` and never called it. The module
    imported, the function was defined, the process exited 0 — and Apify
    reported the run SUCCEEDED with an empty dataset. A green tick over nothing
    built is worse than a crash, because nothing draws attention to it.

    Checked against the Dockerfile's actual CMD rather than an assumed one, so
    renaming the entry point without updating the image fails here.
    """
    tree = ast.parse(ACTOR_MAIN)
    guarded_calls = [
        node for node in tree.body
        if isinstance(node, ast.If)
        and ast.unparse(node.test) == "__name__ == '__main__'"
        for stmt in ast.walk(node)
        if isinstance(stmt, ast.Call) and "run" in ast.unparse(stmt.func)
    ]
    assert guarded_calls, "the module defines main() but never runs it"

    cmd = next(
        line for line in ACTOR_DOCKERFILE.splitlines()
        if line.strip().startswith("CMD")
    )
    assert "src.actor.main" in cmd, f"the image does not run this module: {cmd}"


def test_the_actor_reuses_the_pipeline_rather_than_reimplementing_it():
    """The point of the Actor is a second front door, not a second product. If
    it ever stops importing the shared graph, the two surfaces can claim the
    same guarantees while building differently."""
    for shared in ("from src.graph.builder import build_graph",
                   "from src.prd.schema import PRD",
                   "from src.ports.generator import get_generator"):
        assert shared in ACTOR_MAIN, f"the Actor no longer shares {shared!r}"


def test_scripts_that_run_on_the_box_are_shipped_with_unix_endings():
    """`git archive` applies working-tree conversion, so a Windows checkout with
    core.autocrlf puts CRLF into the tarball that is shipped to Linux.

    `#!/bin/sh\\r` fails as `set: Illegal option -`, which names neither the file
    nor the reason, and it only happens on the box — every local test passes.
    Checked against `git archive` itself rather than the file on disk, because
    the working copy is allowed to hold CRLF; what matters is what ships.
    """
    archived = subprocess.run(
        ["git", "archive", "--format=tar", "HEAD"],
        cwd=str(ROOT), capture_output=True, check=True,
    ).stdout

    with tarfile.open(fileobj=io.BytesIO(archived)) as tar:
        shipped = [
            member for member in tar.getmembers()
            if member.isfile() and member.name.endswith((".sh", ".yml", "Caddyfile"))
        ]
        assert shipped, "no shell or config files found; this test is matching nothing"
        for member in shipped:
            body = tar.extractfile(member).read()
            assert b"\r\n" not in body, (
                f"{member.name} ships with CRLF; it will not run on the Linux box"
            )


def test_the_switch_script_and_the_deployment_check_agree_on_every_chain():
    """The chain constants are written down twice — once in the script that sets
    them and once in the check that refuses a deployment where they disagree.

    Two copies of a value drift, and this pair drifts silently in the worst
    direction: the switch script writes the config, the check validates it, so a
    matching pair of wrong values passes. Both are compared against the same
    table here, which is the one in `verify_deployment.py`.
    """
    sys.path.insert(0, str(ROOT))
    from scripts.verify_deployment import KNOWN_NETWORKS

    script = (ROOT / "deploy" / "switch_network.sh").read_text(encoding="utf-8")
    # Only the `set_var` lines: the header comment names both domain values
    # while explaining the trap, and a substring check would read those.
    settings = re.findall(r'^\s*set_var\s+"?(\w+)"?\s+"?([^"\n]+?)"?\s*$', script, re.M)

    for network, expected in KNOWN_NETWORKS.items():
        block = re.search(
            rf"^\s*{re.escape(network)}\)(.*?)^\s*;;", script, re.M | re.S
        )
        assert block, f"{network} has no branch in switch_network.sh"
        written = dict(re.findall(
            r'^\s*set_var\s+"?(\w+)"?\s+"?([^"\n]+?)"?\s*$', block.group(1), re.M
        ))
        assert written.get("X402_CHAIN_ID") == str(expected["chain_id"])
        assert written.get("X402_TOKEN_CONTRACT") == expected["contract"]
        assert written.get("X402_DOMAIN_NAME") == expected["domain_name"], (
            f"{network}: script writes {written.get('X402_DOMAIN_NAME')!r}, "
            f"the deployment check requires {expected['domain_name']!r}"
        )

    assert settings, "no set_var lines parsed; this test is matching nothing"


def test_secrets_never_reach_the_image_or_a_commit():
    """Key files hold funded private keys and `.env` the Anthropic key, and all
    of them sit in the build context directory.

    Matched with real pathspecs rather than by substring: the patterns are globs
    now, so `".x402-testnet.json" in text` would report a file as ignored on the
    strength of a filename mentioned in a comment — which is how four separate
    checks in this repo have already been fooled. A mainnet payer file is the
    case that matters; it is created after these lines are written, and nothing
    prompts anyone to come back and add it.
    """
    named = [".env", ".env.deploy", ".x402-testnet.json", ".x402-mainnet.json"]

    for ignore_file in (".dockerignore", ".gitignore"):
        patterns = [
            line.strip()
            for line in (ROOT / ignore_file).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        for secret in named:
            assert any(fnmatch(secret, pattern) for pattern in patterns), \
                f"{secret} is not excluded by {ignore_file}"
