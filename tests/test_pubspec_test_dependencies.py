"""The harness declaring its own dependencies.

The QA phase writes integration tests into every generated app. They import
`package:integration_test/...`, which resolves only if the pubspec says so.
`TemplateGenerator`'s template happens to say so, which is why this went
unnoticed until the first Claude-generated app analysed with:

    error - Target of URI doesn't exist:
    'package:integration_test/integration_test_driver.dart'

The real Claude pubspec that produced that error is the fixture below, copied
verbatim, because a hand-written one would have been written by someone who
already knew what the bug was.
"""

from __future__ import annotations

import yaml

from src.ports.pubspec import ensure_test_dependencies

# Verbatim from generated_apps/nav_flow_check, the app whose analysis failed.
CLAUDE_PUBSPEC = """name: sprawl
description: Eight screens with a navigation graph between them.
publish_to: 'none'

environment:
  sdk: '>=3.0.0 <4.0.0'
  flutter: '>=3.10.0'

dependencies:
  flutter:
    sdk: flutter
  flutter_riverpod: ^2.4.0
  firebase_core: ^2.24.0
  cloud_firestore: ^4.14.0
  go_router: ^12.0.0

dev_dependencies:
  flutter_test:
    sdk: flutter
  flutter_lints: ^2.0.0

flutter:
  uses-material-design: true
"""


def _dev_deps(pubspec: str) -> dict:
    return yaml.safe_load(pubspec)["dev_dependencies"]


def test_the_dependency_is_added_to_real_claude_output():
    """The exact input that failed, and the assertion that it no longer does."""
    result = ensure_test_dependencies(CLAUDE_PUBSPEC)

    assert _dev_deps(result)["integration_test"] == {"sdk": "flutter"}


def test_the_result_is_still_valid_yaml_with_everything_else_intact():
    """Inserting into text rather than round-tripping YAML keeps the
    generator's formatting and comments — but must not corrupt the document."""
    parsed = yaml.safe_load(ensure_test_dependencies(CLAUDE_PUBSPEC))

    assert parsed["name"] == "sprawl"
    assert parsed["dependencies"]["go_router"] == "^12.0.0"
    assert parsed["dev_dependencies"]["flutter_lints"] == "^2.0.0"
    assert parsed["dev_dependencies"]["flutter_test"] == {"sdk": "flutter"}
    assert parsed["flutter"]["uses-material-design"] is True


def test_it_is_the_sdk_dependency_not_a_version():
    """`integration_test` ships with the Flutter SDK. A version constraint does
    not resolve, so getting this wrong would trade one broken pubspec for
    another."""
    assert "  integration_test:\n    sdk: flutter\n" in ensure_test_dependencies(
        CLAUDE_PUBSPEC
    )


def test_an_existing_declaration_is_left_alone():
    """TemplateGenerator already declares it. Adding a second key would make the
    pubspec invalid, which would break the one generator that was working."""
    already = CLAUDE_PUBSPEC.replace(
        "dev_dependencies:\n",
        "dev_dependencies:\n  integration_test:\n    sdk: flutter\n",
    )

    assert ensure_test_dependencies(already) == already


def test_a_pubspec_with_no_dev_dependencies_gets_the_section():
    without = CLAUDE_PUBSPEC.replace(
        "dev_dependencies:\n  flutter_test:\n    sdk: flutter\n  flutter_lints: ^2.0.0\n\n",
        "",
    )
    assert "dev_dependencies" not in without

    result = ensure_test_dependencies(without)

    assert _dev_deps(result)["integration_test"] == {"sdk": "flutter"}
    assert yaml.safe_load(result)["flutter"]["uses-material-design"] is True


def test_a_mention_in_a_comment_is_not_a_declaration():
    """Substring matching would see the word and skip the real insertion,
    leaving the app exactly as broken as before."""
    commented = CLAUDE_PUBSPEC.replace(
        "dev_dependencies:\n", "# integration_test: see the QA phase\ndev_dependencies:\n"
    )

    assert _dev_deps(ensure_test_dependencies(commented))["integration_test"] == {
        "sdk": "flutter"
    }
