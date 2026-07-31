"""What a build costs.

`X402_PRICE_ATOMIC` was inherited from a test config and nobody could say
whether $0.50 covered a build, because nothing recorded consumption. These tests
cover the accounting that answers it — and the two ways such a number goes
quietly wrong: counting cached tokens at the full input rate (overstates cost by
close to an order of magnitude on the repair loop, which re-sends a large stable
prefix), and pricing a promotional rate as though it were permanent.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from src.ports.usage import PRICES, Usage


@dataclass
class FakeUsage:
    """The shape the SDK returns on `message.usage`."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


def test_counts_accumulate_across_the_calls_that_make_one_app():
    """A build is planning + GenUI + Logic + repairs. Per-call figures answer
    the wrong question; the buyer paid once, for all of them."""
    usage = Usage()
    usage.add(FakeUsage(input_tokens=1000, output_tokens=500), "claude-sonnet-5")
    usage.add(FakeUsage(input_tokens=2000, output_tokens=800), "claude-sonnet-5")

    assert usage.calls == 2
    assert usage.input_tokens == 3000
    assert usage.output_tokens == 1300
    assert usage.total_tokens == 4300


def test_cached_tokens_are_counted_separately_from_fresh_ones():
    """A cache read bills at about a tenth of the input rate. Folding them into
    `input_tokens` would inflate the cost of exactly the workload this service
    has — a repair loop re-sending a large, stable prompt prefix."""
    usage = Usage()
    usage.add(
        FakeUsage(input_tokens=100, cache_read_input_tokens=90_000),
        "claude-sonnet-5",
    )

    assert usage.input_tokens == 100
    assert usage.cache_read_tokens == 90_000
    # 90k cached at 0.1x is worth 9k fresh — nowhere near 90k.
    assert usage.cost_usd("2026-07-31") == pytest.approx(
        (100 * 2.00 + 90_000 * 2.00 * 0.1) / 1_000_000
    )


def test_sonnet_introductory_pricing_expires():
    """Sonnet 5 lists $3/$15 with an introductory $2/$10 through 2026-08-31. A
    cost basis measured during the promotion understates the September cost by
    half again, which is the difference between a margin and a loss."""
    usage = Usage(model="claude-sonnet-5", input_tokens=1_000_000, output_tokens=1_000_000)

    during = usage.cost_usd("2026-08-31")
    after = usage.cost_usd("2026-09-01")

    assert during == pytest.approx(2.00 + 10.00)
    assert after == pytest.approx(3.00 + 15.00)
    assert after > during


def test_opus_costs_what_the_published_rate_says():
    usage = Usage(model="claude-opus-5", input_tokens=1_000_000, output_tokens=1_000_000)

    assert usage.cost_usd("2026-07-31") == pytest.approx(5.00 + 25.00)


def test_an_unknown_model_still_reports_tokens():
    """Withholding the dollar figure is honest; withholding the token counts
    too would make an unpriced model unmeasurable, which is worse."""
    usage = Usage(model="some-model-shipped-after-this-was-written", input_tokens=1000)

    assert usage.cost_usd() is None
    assert usage.public()["total_tokens"] == 1000
    assert usage.public()["estimated_cost_usd"] is None


def test_missing_usage_fields_do_not_break_a_build():
    """`usage` is an SDK object whose field set grows. A build must not fail
    because accounting met a counter it did not recognise."""
    class Sparse:
        input_tokens = 50

    usage = Usage()
    usage.add(Sparse(), "claude-opus-5")

    assert usage.input_tokens == 50
    assert usage.output_tokens == 0
    assert usage.cache_read_tokens == 0


def test_cache_writes_cost_more_than_fresh_input():
    """A cache write is ~1.25x input for the default TTL. Counting it as 1x
    would make caching look free, which is the wrong signal when deciding
    whether the repair loop's prefix is worth caching at all."""
    write_only = Usage(model="claude-opus-5", cache_write_tokens=1_000_000)
    fresh_only = Usage(model="claude-opus-5", input_tokens=1_000_000)

    assert write_only.cost_usd() > fresh_only.cost_usd()
    assert write_only.cost_usd() == pytest.approx(5.00 * 1.25)


def test_every_priced_model_has_both_rates():
    """A half-filled row would price output at zero and read as a suspiciously
    cheap model rather than as a missing number."""
    for model, rates in PRICES.items():
        assert rates.get("input"), model
        assert rates.get("output"), model
        if "intro_until" in rates:
            assert "intro_input" in rates and "intro_output" in rates, model
