"""Hard spend caps, enforced before every API call.

For each call:
  1. estimate the input size (deliberately high: 3 characters per token);
  2. work out how many output tokens are still affordable under the run cap and the day cap,
     at the worst rates the call could be billed at (fallback models included);
  3. if fewer than `min_output_tokens` fit, stop cleanly; otherwise shrink `max_tokens` to fit
     and reserve that worst case in the ledger (atomically, so parallel runs can't overshoot);
  4. after the call, replace the reservation with the real cost from `usage`.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from costagent import pricing
from costagent.pricing import Usage
from costagent.store import Store, utc_now

CHARS_PER_TOKEN = 3  # English and code average nearer 4; 3 over-estimates on purpose


class BudgetExhausted(Exception):
    def __init__(self, scope: str, remaining: Decimal):
        super().__init__(f"{scope} budget exhausted (remaining ${remaining:.4f})")
        self.scope, self.remaining = scope, remaining


def estimate_tokens(*parts: Any) -> int:
    def default(o):
        if hasattr(o, "model_dump"):
            return o.model_dump()
        if hasattr(o, "__dict__"):
            return vars(o)
        return str(o)

    text = json.dumps(parts, default=default, ensure_ascii=False)
    return len(text) // CHARS_PER_TOKEN + 1


@dataclass
class Grant:
    reservation_id: int
    model: str
    max_tokens: int
    reserved_usd: Decimal


@dataclass
class Spent:
    usd: Decimal = Decimal("0")
    calls: int = 0
    usage_by_model: dict[str, Usage] = field(default_factory=dict)

    def add(self, model: str, usage: Usage, usd: Decimal) -> None:
        self.usd += usd
        self.calls += 1
        prev = self.usage_by_model.get(model, Usage())
        self.usage_by_model[model] = Usage(
            prev.input_tokens + usage.input_tokens,
            prev.output_tokens + usage.output_tokens,
            prev.cache_write_tokens + usage.cache_write_tokens,
            prev.cache_read_tokens + usage.cache_read_tokens,
        )


class RunBudget:
    def __init__(self, store: Store, run_cap: Decimal, day_cap: Decimal, min_output_tokens: int, clock=utc_now):
        self.store, self.run_cap, self.day_cap = store, run_cap, day_cap
        self.min_output_tokens, self.clock = min_output_tokens, clock
        self.run_id = uuid.uuid4().hex
        self.spent = Spent()
        self._open: dict[int, Decimal] = {}  # reservations not yet settled, this run
        self._lock = asyncio.Lock()

    @property
    def run_committed(self) -> Decimal:
        return self.spent.usd + sum(self._open.values(), Decimal("0"))

    async def reserve(self, purpose: str, model: str, est_input_tokens: int, wanted_max_tokens: int) -> Grant:
        async with self._lock:
            run_left = self.run_cap - self.run_committed
            day_left = self.day_cap - await asyncio.to_thread(self.store.day_committed, self.clock().date().isoformat())
            left = min(run_left, day_left)
            affordable = pricing.affordable_output_tokens(model, est_input_tokens, left)
            max_tokens = min(wanted_max_tokens, affordable)
            if max_tokens < self.min_output_tokens:
                raise BudgetExhausted("run" if run_left <= day_left else "day", max(left, Decimal("0")))
            usd = pricing.worst_case_cost(model, est_input_tokens, max_tokens)
            rid = await asyncio.to_thread(
                self.store.reserve, self.run_id, purpose, model, usd, self.day_cap, self.clock()
            )
            if rid is None:  # another process took the day's budget in the meantime
                raise BudgetExhausted("day", Decimal("0"))
            self._open[rid] = usd
            return Grant(rid, model, max_tokens, usd)

    async def settle(self, grant: Grant, served_model: str, api_usage) -> Decimal:
        usage = Usage.from_api(api_usage)
        usd = pricing.cost(usage, served_model or grant.model)
        await asyncio.to_thread(self.store.settle, grant.reservation_id, served_model or grant.model, usd, usage)
        async with self._lock:
            self._open.pop(grant.reservation_id, None)
            self.spent.add(served_model or grant.model, usage, usd)
        return usd

    async def settle_reserved(self, grant: Grant) -> None:
        """The call may or may not have been billed (a timeout or dropped connection): count the
        reserved worst case as spent, so the cap stays safe."""
        await asyncio.to_thread(self.store.settle, grant.reservation_id, grant.model, grant.reserved_usd, Usage())
        async with self._lock:
            self._open.pop(grant.reservation_id, None)
            self.spent.add(grant.model, Usage(), grant.reserved_usd)

    async def release(self, grant: Grant) -> None:
        """The call failed before anything was billed: record it as costing nothing."""
        await asyncio.to_thread(self.store.settle, grant.reservation_id, grant.model, Decimal("0"), Usage())
        async with self._lock:
            self._open.pop(grant.reservation_id, None)
