"""Token cost calculation and run-level usage totals."""

from __future__ import annotations

import threading

from pydantic import BaseModel, Field, PrivateAttr

from ticketideai.config.models import Pricing
from ticketideai.llm.types import Usage


def cost_of(usage: Usage, pricing: Pricing) -> tuple[float, float]:
    """Return (input_cost, output_cost) in USD for one call."""
    return (
        usage.input_tokens * pricing.input_per_mtok / 1_000_000,
        usage.output_tokens * pricing.output_per_mtok / 1_000_000,
    )


class RoleUsage(BaseModel):
    """Usage totals for one role or for the whole run."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float = 0.0

    @property
    def total_tokens(self) -> int:
        """Sum of input and output tokens."""
        return self.input_tokens + self.output_tokens

    def add(self, usage: Usage, cost_usd: float) -> None:
        """Add one call to these totals."""
        self.calls += 1
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.reasoning_tokens += usage.reasoning_tokens
        self.cost_usd += cost_usd


class RunUsage(RoleUsage):
    """Run totals plus a per-role breakdown. Safe to update from several threads."""

    by_role: dict[str, RoleUsage] = Field(default_factory=dict)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def record(self, usage: Usage, cost_usd: float = 0.0, role: str = "default") -> None:
        """Add one call's usage and cost to the run and role totals."""
        with self._lock:
            self.add(usage, cost_usd)
            self.by_role.setdefault(role, RoleUsage()).add(usage, cost_usd)
