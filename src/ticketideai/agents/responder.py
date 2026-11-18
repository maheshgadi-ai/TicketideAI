"""Responder agent: writes the customer reply from the recorded outcome."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from ticketideai.agents.base import BaseAgent
from ticketideai.llm import LLMError, LLMProvider
from ticketideai.observability.tracing import Tracer
from ticketideai.schemas.workflow import CustomerReply, ReplyFacts
from ticketideai.tools.reply_guard import fallback_reply, reply_problems

logger = logging.getLogger(__name__)
PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "responder.md"


@dataclass(frozen=True)
class ResponderResult:
    """The final reply, plus what the guard saw in the model's draft."""

    reply: str
    replaced: bool
    draft: str = ""
    problems: list[str] = field(default_factory=list)


class ResponderAgent(BaseAgent[ReplyFacts, CustomerReply]):
    """Draft a reply and replace it with a safe template if it is unsupported."""

    name = "responder"
    role = "responder"

    def __init__(self, provider: LLMProvider, tracer: Tracer | None = None) -> None:
        super().__init__(provider, PROMPT_PATH, tracer)

    def respond(self, facts: ReplyFacts) -> ResponderResult:
        """Return the reply to send and whether the guard replaced the model's draft."""
        try:
            draft = self.run(facts, CustomerReply).body
        except LLMError:
            logger.warning("responder call failed; using the template reply", exc_info=True)
            return ResponderResult(fallback_reply(facts), True, problems=["responder call failed"])
        problems = reply_problems(draft, facts)
        if problems:
            logger.warning("responder draft replaced", extra={"problems": problems})
            return ResponderResult(fallback_reply(facts), True, draft, problems)
        return ResponderResult(draft, False, draft)
