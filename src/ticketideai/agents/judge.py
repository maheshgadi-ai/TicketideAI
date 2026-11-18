"""Judge agent: checks a customer reply against the recorded outcome."""

from __future__ import annotations

from pathlib import Path

from ticketideai.agents.base import BaseAgent
from ticketideai.llm import LLMProvider
from ticketideai.observability.tracing import Tracer
from ticketideai.schemas.agent_evals import JudgeInput, JudgeVerdict

PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "judge.md"


class GroundednessJudge(BaseAgent[JudgeInput, JudgeVerdict]):
    """Decide whether every claim in a reply is supported by the evidence."""

    name = "judge"
    role = "judge"

    def __init__(self, provider: LLMProvider, tracer: Tracer | None = None) -> None:
        super().__init__(provider, PROMPT_PATH, tracer)

    def judge(self, evidence: JudgeInput) -> JudgeVerdict:
        """Return the verdict for one reply."""
        return self.run(evidence, JudgeVerdict)
