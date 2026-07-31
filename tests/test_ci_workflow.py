"""The CI workflow, checked against the claims it is supposed to be making.

None of this can prove the workflow runs — there is no git remote, so it has
never executed. What it can prove is that the workflow still says what it was
written to say, which is the part that rots: a Flutter version bumped in the
Dockerfile and not here, or an eval sweep quietly switched to the offline stub
analyzer, would both leave CI green while checking something weaker than
advertised.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
RAW = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
WORKFLOW = yaml.safe_load(RAW)
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")


def _steps(job: str) -> list[dict]:
    return WORKFLOW["jobs"][job]["steps"]


def _run_commands(job: str) -> str:
    return "\n".join(step.get("run", "") for step in _steps(job))


def test_the_workflow_is_valid_yaml_with_the_jobs_it_claims():
    assert set(WORKFLOW["jobs"]) == {"tests", "eval-sweep"}


def test_the_suite_runs_on_linux_as_well_as_windows():
    """The reason this file exists. Four resolvers picked the Windows Flutter
    launcher and shipped to a paying buyer, invisible to 299 tests because every
    one of them ran on Windows. The Linux leg is not redundancy."""
    assert set(WORKFLOW["jobs"]["tests"]["strategy"]["matrix"]["os"]) == {
        "ubuntu-latest",
        "windows-latest",
    }


def test_a_failure_on_one_platform_does_not_cancel_the_other():
    """A Windows-only or Linux-only failure is the interesting case, and
    fail-fast would hide which platforms were actually affected."""
    assert WORKFLOW["jobs"]["tests"]["strategy"]["fail-fast"] is False


def test_the_sweep_uses_the_real_analyzer():
    """`evals/run.py` defaults to `--analyzer stub`, which never invokes the
    Flutter toolchain. A sweep that silently ran offline would go green without
    testing the one thing this project promises: that the output compiles."""
    commands = _run_commands("eval-sweep")

    assert "evals/run.py" in commands
    assert "--analyzer dart" in commands


def test_ci_and_the_image_pin_the_same_flutter():
    """Two places now name a Flutter version. If they drift, CI stops testing
    what the deployment actually builds with."""
    in_ci = re.search(r'flutter-version:\s*"([\d.]+)"', RAW)
    in_image = re.search(r"FLUTTER_VERSION=([\d.]+)", DOCKERFILE)

    assert in_ci and in_image
    assert in_ci.group(1) == in_image.group(1) == "3.44.8"


def test_the_sweep_keeps_its_output_when_it_fails():
    """A red sweep is worth nothing without the code that failed to compile."""
    upload = next(
        step for step in _steps("eval-sweep")
        if "upload-artifact" in str(step.get("uses", ""))
    )

    assert upload["if"] == "failure()"
    assert upload["with"]["path"].startswith("generated_apps/evals")


def test_no_step_needs_a_secret():
    """The template generator costs nothing and needs no API key. A CI that
    required ANTHROPIC_API_KEY would spend real money per push, and would fail
    for anyone who forked this."""
    assert "secrets." not in RAW
    assert "ANTHROPIC_API_KEY" not in RAW
