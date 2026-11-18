"""Streamlit customer chat and staff panel for TicketideAI."""

from __future__ import annotations

import streamlit as st

from ticketideai.config import Settings
from ticketideai.interfaces.app_runtime import (
    AppRuntime,
    build_runtime,
    customer_emails,
    inventory_table,
    orders_table,
)
from ticketideai.interfaces.demo import DEMO_REQUESTS
from ticketideai.schemas.store import ApprovalItem
from ticketideai.schemas.workflow import WorkflowResult
from ticketideai.tools.errors import StoreError

OUTCOME_LABELS = {
    "committed": "Committed",
    "answered": "Answered",
    "escalated": "Waiting for staff review",
    "refused": "Refused",
    "needs_clarification": "Needs clarification",
    "failed_safely": "Failed safely",
}


@st.cache_resource
def load_runtime() -> AppRuntime:
    """Build the services once per server process."""
    return build_runtime(Settings())


def main() -> None:
    """Render the application."""
    st.set_page_config(page_title="TicketideAI", layout="wide")
    runtime = load_runtime()
    st.title("TicketideAI")
    st.caption("Ridgeline Outfitters support agent: plans in code, commits only checked effects.")
    if not runtime.live:
        st.info(
            "Demo mode: API keys are not set, so plans and replies are scripted. The sandbox, "
            "policy checks, commit, and approval queue are real and use a scratch store."
        )
    chat_tab, staff_tab = st.tabs(["Customer chat", "Staff panel"])
    with chat_tab:
        _render_chat(runtime)
    with staff_tab:
        _render_staff(runtime)


def _render_chat(runtime: AppRuntime) -> None:
    history = st.session_state.setdefault("history", [])
    request = _live_request(runtime) if runtime.live else _demo_request()
    if request is not None:
        email, message = request
        result = runtime.handle(email, message)
        st.session_state["last_result"] = result
        history.append((email, message, result))
    for email, message, result in history:
        with st.chat_message("user"):
            st.caption(email)
            st.write(message)
        with st.chat_message("assistant"):
            st.caption(OUTCOME_LABELS[result.outcome])
            st.write(result.reply)


def _live_request(runtime: AppRuntime) -> tuple[str, str] | None:
    email = st.selectbox("Customer", customer_emails(runtime.database_url), key="customer")
    message = st.chat_input("How can we help?")
    return (email, message) if message else None


def _demo_request() -> tuple[str, str] | None:
    labels = [item.label for item in DEMO_REQUESTS]
    label = st.selectbox("Sample request", labels, key="sample")
    chosen = next(item for item in DEMO_REQUESTS if item.label == label)
    st.write(f"**{chosen.customer_email}:** {chosen.message}")
    if st.button("Send request", key="send-sample"):
        return chosen.customer_email, chosen.message
    return None


def _render_staff(runtime: AppRuntime) -> None:
    latest, queue, orders, inventory = st.tabs(
        ["Latest request", "Approval queue", "Orders", "Inventory"]
    )
    with latest:
        _render_latest(st.session_state.get("last_result"))
    with queue:
        _render_queue(runtime)
    with orders:
        st.dataframe(orders_table(runtime.database_url), hide_index=True, width="stretch")
    with inventory:
        st.dataframe(inventory_table(runtime.database_url), hide_index=True, width="stretch")


def _render_latest(result: WorkflowResult | None) -> None:
    if result is None:
        st.write("Send a customer request to see its plan, change set, and policy decision.")
        return
    outcome, policy, reflections = st.columns(3)
    outcome.metric("Outcome", OUTCOME_LABELS[result.outcome])
    policy.metric("Policy decision", (result.policy_action or "not evaluated").title())
    reflections.metric("Plan corrections", result.reflection_attempts)
    st.subheader("Plan code")
    st.code(result.plan_source or "No plan was run.", language="python")
    st.subheader("Change set")
    st.text(result.diff_text or "No changes proposed.")
    st.subheader("Policy findings")
    if result.findings:
        for finding in result.findings:
            st.write(f"**{finding.action.title()}** ({finding.rule}): {finding.message}")
    else:
        st.write("No policy rule fired.")
    st.subheader("Database diff")
    st.json(result.diff or {"tables": {}}, expanded=False)


def _render_queue(runtime: AppRuntime) -> None:
    notice = st.session_state.pop("queue_notice", None)
    if notice:
        st.success(notice)
    reviewer = st.text_input("Reviewer name", key="reviewer")
    pending = runtime.pending()
    if not pending:
        st.write("No change sets are waiting for review.")
    for item in pending:
        _render_item(runtime, item, reviewer)


def _render_item(runtime: AppRuntime, item: ApprovalItem, reviewer: str) -> None:
    with st.container(border=True):
        st.markdown(f"**Item {item.id}** for {item.customer_email}")
        st.write(item.message)
        st.write("Why it needs review: " + "; ".join(item.reasons))
        st.text(item.diff_text)
        with st.expander("Plan code"):
            st.code(item.plan_source, language="python")
        approve, reject = st.columns(2)
        if approve.button("Approve and commit", key=f"approve-{item.id}"):
            _decide(lambda: runtime.approve(item.id, reviewer))
        if reject.button("Reject", key=f"reject-{item.id}"):
            _decide(lambda: runtime.reject(item.id, reviewer))


def _decide(action) -> None:
    try:
        outcome = action()
    except StoreError as exc:
        st.error(str(exc))
        return
    st.session_state["queue_notice"] = (
        f"Item {outcome.item.id}: {outcome.item.status}. {outcome.message}. "
        f"Customer notified: {outcome.item.customer_notice}"
    )
    st.rerun()


main()
