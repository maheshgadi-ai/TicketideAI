"""Deterministic checks and safe templates for customer replies."""

from __future__ import annotations

import re

from ticketideai.schemas.workflow import ReplyFacts

_COMPLETED_WORDS = (
    r"(?:issued|processed|completed?|refunded|credited|sent|approved|reimbursed|created)\b"
)
_SENTENCES = re.compile(r"(?<=[.!?\n])\s+")
_REFUND_TOPIC = re.compile(r"\b(refund\w*|money|credit\w*|reimburs\w*)\b", re.IGNORECASE)
_RETURN_TOPIC = re.compile(r"\breturn\w*\b", re.IGNORECASE)
_DONE_CLAIM = re.compile(
    r"(?:\b(?:has|have|had|was|were|is|are)|'ve)\s+(?:been\s+|now\s+|already\s+)*"
    + _COMPLETED_WORDS
    + r"|\bwe(?:'ve| have)?\s+(?:just\s+|now\s+|already\s+)?"
    r"(?:refunded|credited|processed|issued)\b"
    r"|\b(?:refund|return)\s+(?:is\s+)?(?:complete|completed|done|finished)\b",
    re.IGNORECASE,
)
_NEGATED_CLAIM = re.compile(
    r"\b(?:no\s+(?:new\s+|additional\s+|such\s+)?"
    r"(?:refunds?|returns?|money|credits?|reimbursements?)|nothing)\s+"
    r"(?:has|have|had|was|were|is|are)\s+(?:been\s+|now\s+|yet\s+|already\s+)*" + _COMPLETED_WORDS,
    re.IGNORECASE,
)
_AMOUNT = re.compile(r"\$\s?(\d[\d,]*(?:\.\d{1,2})?)")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_NOTHING_PROPOSED = {"answered", "refused", "needs_clarification", "failed_safely"}


def reply_problems(reply: str, facts: ReplyFacts) -> list[str]:
    """Return reasons a reply is not supported by the recorded facts."""
    problems: list[str] = []
    if not reply.strip():
        problems.append("reply is empty")
    if not facts.refunds_issued and _claims_completion(reply, _REFUND_TOPIC):
        problems.append("reply claims a refund is complete but none was committed")
    if not facts.returns_created and _claims_completion(reply, _RETURN_TOPIC):
        problems.append("reply claims a return is complete but none was committed")
    return problems + _amount_problems(reply, facts)


def fallback_reply(facts: ReplyFacts) -> str:
    """Return a template reply built from recorded facts and checked text only."""
    if facts.outcome == "committed":
        return " ".join(["Your request is complete.", _committed_details(facts)]).strip()
    if facts.outcome == "escalated":
        return (
            "Your request is waiting for review by a member of our team, and nothing on your "
            "order has changed yet. We will contact you once the review is finished."
        )
    if facts.outcome == "failed_safely":
        return (
            "We could not complete your request safely, and nothing on your order was changed. "
            "Please try again in a moment."
        )
    if facts.outcome == "needs_clarification":
        question = _checked(facts.clarifying_question, facts)
        default = "Could you share your order number and the item?"
        return f"We need a little more information to help. {question or default}"
    if facts.outcome == "answered":
        summary = _checked(str(facts.answer.get("summary", "")), facts)
        return summary or "We looked into your request and no changes were needed."
    reasons = [text for text in map(lambda item: _checked(item, facts), facts.reasons) if text]
    detail = f" {_sentence(reasons[0])}" if reasons else ""
    return f"We are unable to complete this request, and no changes were made.{detail}"


def _claims_completion(reply: str, topic: re.Pattern[str]) -> bool:
    return any(
        topic.search(sentence) and _asserts_done(sentence) for sentence in _SENTENCES.split(reply)
    )


def _asserts_done(sentence: str) -> bool:
    return _DONE_CLAIM.search(_NEGATED_CLAIM.sub(" ", sentence)) is not None


def _amount_problems(reply: str, facts: ReplyFacts) -> list[str]:
    known = _known_amounts(facts)
    quoted = _customer_amounts(facts) if facts.outcome in _NOTHING_PROPOSED else set()
    problems: list[str] = []
    for sentence in _SENTENCES.split(reply):
        for amount in _AMOUNT.findall(sentence):
            value = _money(amount)
            restated = value in quoted and not _asserts_done(sentence)
            if value not in known and not restated:
                problems.append(f"reply states an unsupported amount: ${amount}")
    return problems


def _known_amounts(facts: ReplyFacts) -> set[float]:
    text = facts.model_dump_json(exclude={"customer_message"}).replace(",", " ")
    return {round(float(number), 2) for number in _NUMBER.findall(text)}


def _customer_amounts(facts: ReplyFacts) -> set[float]:
    return {_money(amount) for amount in _AMOUNT.findall(facts.customer_message)}


def _money(amount: str) -> float:
    return round(float(amount.replace(",", "")), 2)


def _checked(text: str | None, facts: ReplyFacts) -> str | None:
    """Return model-written text only when it passes the same checks as a reply."""
    cleaned = (text or "").strip()
    return cleaned if cleaned and not reply_problems(cleaned, facts) else None


def _sentence(text: str) -> str:
    cleaned = text.strip().rstrip(".!? ")
    return f"{cleaned[:1].upper()}{cleaned[1:]}."


def _committed_details(facts: ReplyFacts) -> str:
    details = [f"Refund of ${r['amount']:.2f} issued." for r in facts.refunds_issued]
    details += [f"Return of {r['quantity']} x {r['sku']} opened." for r in facts.returns_created]
    details += [f"Order {s['order_id']} is now {s['status']}." for s in facts.status_changes]
    return " ".join(details)
