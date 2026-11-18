from ticketideai.config import Pricing
from ticketideai.llm import RunUsage, Usage
from ticketideai.llm.pricing import cost_of


def test_cost_of() -> None:
    pricing = Pricing(input_per_mtok=1.0, output_per_mtok=4.0)
    assert cost_of(Usage(input_tokens=2_000_000, output_tokens=500_000), pricing) == (2.0, 2.0)
    assert cost_of(Usage(input_tokens=10, output_tokens=10), Pricing()) == (0.0, 0.0)


def test_run_usage_totals_and_breakdown() -> None:
    run = RunUsage()
    run.record(Usage(input_tokens=100, output_tokens=50), 0.5, role="responder")
    run.record(Usage(input_tokens=10, output_tokens=5), 0.1, role="judge")
    run.record(Usage(input_tokens=1, output_tokens=1), role="responder")

    assert (run.calls, run.total_tokens) == (3, 167)
    assert run.cost_usd == 0.6
    assert run.by_role["responder"].calls == 2
    assert run.by_role["judge"].cost_usd == 0.1
