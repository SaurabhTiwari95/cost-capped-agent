"""Model prices and cost arithmetic.

Prices are USD per million tokens, from Anthropic's published price list as of 2026-10-06.
Check https://platform.claude.com/docs/en/about-claude/pricing before relying on them, and
override in your own fork if they change. Cache writes use the 5-minute TTL rate (1.25x input).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

MILLION = Decimal(1_000_000)
CACHE_WRITE_MULTIPLIER = Decimal("1.25")
HAIKU_LONG_PROMPT_TOKENS = 100_000  # Haiku 5.5 is priced higher above this prompt size


@dataclass(frozen=True)
class Price:
    input: Decimal
    output: Decimal
    cache_read: Decimal

    @property
    def cache_write(self) -> Decimal:
        return self.input * CACHE_WRITE_MULTIPLIER


def _p(i: str, o: str, r: str) -> Price:
    return Price(Decimal(i), Decimal(o), Decimal(r))


PRICES: dict[str, Price] = {
    "claude-opus-5-5": _p("4.00", "20.00", "0.20"),
    "claude-opus-5": _p("5.00", "25.00", "0.50"),
    "claude-opus-4-8": _p("5.00", "25.00", "0.50"),
    "claude-sonnet-5-5": _p("2.00", "10.00", "0.20"),
    "claude-haiku-5-5": _p("0.10", "0.50", "0.01"),
    "claude-haiku-5-5:long": _p("0.50", "2.50", "0.05"),
}

# With fallbacks: "default", a declined request may be finished by another model at its rates.
POSSIBLE_FALLBACKS: dict[str, tuple[str, ...]] = {"claude-opus-5-5": ("claude-opus-5", "claude-opus-4-8")}

# Anything not in the table is priced at the most expensive rates we know: over-counting is safe.
_WORST = Price(
    max(p.input for p in PRICES.values()),
    max(p.output for p in PRICES.values()),
    max(p.cache_read for p in PRICES.values()),
)


def price_for(model: str, prompt_tokens: int = 0) -> Price:
    if model == "claude-haiku-5-5" and prompt_tokens > HAIKU_LONG_PROMPT_TOKENS:
        return PRICES["claude-haiku-5-5:long"]
    return PRICES.get(model, _WORST)


def worst_price(model: str, prompt_tokens: int = 0) -> Price:
    """The highest rates a request to `model` could be billed at, fallbacks included."""
    candidates = [price_for(model, prompt_tokens)] + [
        price_for(m, prompt_tokens) for m in POSSIBLE_FALLBACKS.get(model, ())
    ]
    return Price(
        max(p.input for p in candidates), max(p.output for p in candidates), max(p.cache_read for p in candidates)
    )


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0

    @classmethod
    def from_api(cls, usage) -> Usage:
        return cls(
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        )

    @property
    def prompt_tokens(self) -> int:
        return self.input_tokens + self.cache_write_tokens + self.cache_read_tokens


def cost(usage: Usage, model: str) -> Decimal:
    p = price_for(model, usage.prompt_tokens)
    usd = (
        usage.input_tokens * p.input
        + usage.cache_write_tokens * p.cache_write
        + usage.cache_read_tokens * p.cache_read
        + usage.output_tokens * p.output
    ) / MILLION
    return usd.quantize(Decimal("0.000001"))


def worst_case_cost(model: str, est_input_tokens: int, max_output_tokens: int) -> Decimal:
    """Most a call could cost: every input token written to cache (the dearest input rate) and every
    output token used. Thinking tokens count as output, and max_tokens bounds them too."""
    p = worst_price(model, est_input_tokens)
    usd = (est_input_tokens * max(p.input, p.cache_write) + max_output_tokens * p.output) / MILLION
    return usd.quantize(Decimal("0.000001"))


def affordable_output_tokens(model: str, est_input_tokens: int, budget_usd: Decimal) -> int:
    """How many output tokens fit in `budget_usd` after paying for the input at worst-case rates."""
    p = worst_price(model, est_input_tokens)
    left = budget_usd * MILLION - est_input_tokens * max(p.input, p.cache_write)
    return max(0, int(left / p.output)) if left > 0 else 0
