"""What a build costs, in tokens and in money.

`X402_PRICE_ATOMIC` was inherited from a test configuration and nobody could say
whether $0.50 covered a build, because nothing recorded what a build consumed.
This does: every model response carries a `usage` block, and the generator
accumulates it across the planning, GenUI, Logic and repair calls that make up
one app.

**Tokens are the fact; money is an estimate.** Token counts come from the API
and are exact. The dollar figure is derived from a rate table that lives in this
repo, and published prices change — treat it as an order-of-magnitude guide for
pricing decisions, not as an invoice. The per-model rates are checked against
Anthropic's published pricing rather than recalled, and the table says when.

**Cached tokens are not billed like fresh ones**, and conflating them overstates
cost by roughly an order of magnitude on a cache-heavy workload: a cache *read*
bills at about a tenth of the input rate, while a cache *write* costs about 1.25x
input for the default five-minute TTL. The repair loop re-sends a large, stable
prompt prefix, so those two lines dominate a multi-repair build and have to be
counted separately.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Published rates in US dollars per million tokens, read from Anthropic's
# pricing on 2026-07-31. `intro_until` records a promotional rate that expires:
# Sonnet 5 lists $3.00/$15.00 with an introductory $2.00/$10.00 through
# 2026-08-31, so a cost basis measured today understates what the same build
# costs in September by half again.
#
# A model missing from this table still gets exact token counts — only the
# dollar estimate is withheld, which is the honest failure rather than silently
# pricing an unknown model at some neighbour's rate.
PRICES: dict[str, dict[str, object]] = {
    "claude-opus-5": {"input": 5.00, "output": 25.00},
    "claude-opus-4-8": {"input": 5.00, "output": 25.00},
    "claude-sonnet-5": {
        "input": 3.00,
        "output": 15.00,
        "intro_input": 2.00,
        "intro_output": 10.00,
        "intro_until": "2026-08-31",
    },
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00},
}

# Multipliers on the model's input rate. Cache reads are the reason this module
# tracks four counters instead of two.
CACHE_READ_MULTIPLIER = 0.1
CACHE_WRITE_MULTIPLIER = 1.25  # five-minute TTL; a one-hour TTL is 2x


@dataclass
class Usage:
    """Token counts for one build, accumulated across every model call."""

    model: str = ""
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def add(self, usage: object, model: str = "") -> None:
        """Fold in one response's `usage` block.

        Reads defensively: `usage` is an SDK object whose field set grows over
        time, and a build must not fail because accounting met a field it did
        not recognise. Missing counters are zero, which understates rather than
        invents.
        """
        if model:
            self.model = model
        self.calls += 1
        self.input_tokens += getattr(usage, "input_tokens", 0) or 0
        self.output_tokens += getattr(usage, "output_tokens", 0) or 0
        self.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0
        self.cache_write_tokens += getattr(usage, "cache_creation_input_tokens", 0) or 0

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )

    def cost_usd(self, on_date: str = "") -> float | None:
        """Estimated dollars, or None when the model's rate is unknown.

        `on_date` (ISO `YYYY-MM-DD`) selects introductory versus standard rates.
        It is a parameter rather than "today" so that pricing a build for a date
        after a promotion ends is a question this can answer, and so the test
        for it does not depend on when the test runs.
        """
        rates = PRICES.get(self.model)
        if rates is None:
            return None

        intro_until = rates.get("intro_until")
        use_intro = bool(intro_until) and (not on_date or on_date <= str(intro_until))
        input_rate = float(
            rates["intro_input"] if use_intro and "intro_input" in rates
            else rates["input"]
        )
        output_rate = float(
            rates["intro_output"] if use_intro and "intro_output" in rates
            else rates["output"]
        )

        per_million = (
            self.input_tokens * input_rate
            + self.output_tokens * output_rate
            + self.cache_read_tokens * input_rate * CACHE_READ_MULTIPLIER
            + self.cache_write_tokens * input_rate * CACHE_WRITE_MULTIPLIER
        )
        return per_million / 1_000_000

    def public(self, on_date: str = "") -> dict[str, object]:
        """What a buyer and an operator both get to see.

        The token counts are reported whatever the model, so an unpriced model
        still yields a usable record — `estimated_cost_usd` is null rather than
        the whole block being absent.
        """
        return {
            "model": self.model or None,
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "total_tokens": self.total_tokens,
            "estimated_cost_usd": self.cost_usd(on_date),
        }
