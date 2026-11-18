"""Streamlit interface tests, run in scripted demo mode."""

from __future__ import annotations

from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from ticketideai.config import Settings
from ticketideai.interfaces.app_runtime import build_runtime, inventory_table, orders_table
from ticketideai.interfaces.demo import DEMO_REQUESTS

APP = Path(__file__).resolve().parents[2] / "src" / "ticketideai" / "interfaces" / "app.py"
LARGE_REFUND = "Large refund needs review"


@pytest.fixture()
def app() -> AppTest:
    st.cache_resource.clear()
    return AppTest.from_file(str(APP), default_timeout=60).run()


def send(app: AppTest, label: str) -> AppTest:
    app.selectbox(key="sample").select(label).run()
    return app.button(key="send-sample").click().run()


def test_app_starts_in_demo_mode_without_keys(app: AppTest) -> None:
    assert not app.exception
    assert any("Demo mode" in item.value for item in app.info)
    assert [tab.label for tab in app.tabs][:2] == ["Customer chat", "Staff panel"]


def test_staff_panel_shows_plan_change_set_and_policy_decision(app: AppTest) -> None:
    send(app, LARGE_REFUND)
    assert not app.exception
    assert "api.issue_refund(2, 238.5)" in app.code[0].value
    assert any("Refund $238.50 on order 2" in item.value for item in app.text)
    assert any(metric.value == "Escalate" for metric in app.metric)
    assert any("refund_threshold" in item.value for item in app.markdown)


def test_human_approval_commits_and_notifies_the_customer(app: AppTest) -> None:
    send(app, LARGE_REFUND)
    app.text_input(key="reviewer").set_value("Dana").run()
    app.button(key="approve-1").click().run()
    assert not app.exception
    assert any("Customer notified: A team member approved" in item.value for item in app.success)
    assert not [button for button in app.button if button.key == "approve-1"]


def test_human_rejection_closes_the_item(app: AppTest) -> None:
    send(app, LARGE_REFUND)
    app.text_input(key="reviewer").set_value("Dana").run()
    app.button(key="reject-1").click().run()
    assert not app.exception
    assert any("rejected" in item.value for item in app.success)


def test_approval_requires_a_reviewer_name(app: AppTest) -> None:
    send(app, LARGE_REFUND)
    app.button(key="approve-1").click().run()
    assert any("reviewer name is required" in item.value for item in app.error)


def test_customer_view_never_offers_approval(app: AppTest) -> None:
    send(app, DEMO_REQUESTS[0].label)
    chat = app.tabs[0]
    assert not [button for button in chat.button if "approve" in (button.key or "")]


def test_tables_show_orders_and_inventory() -> None:
    runtime = build_runtime(Settings(_env_file=None))
    orders = orders_table(runtime.database_url)
    assert orders[0] == {
        "order": 1,
        "customer": "customer1@example.com",
        "status": "delivered",
        "ordered": "2026-11-12",
        "paid": 111.25,
        "refunded": 0.0,
    }
    assert len(inventory_table(runtime.database_url)) == 40


def test_runtime_is_scripted_when_a_key_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "only-one-key")
    assert build_runtime(Settings(_env_file=None)).live is False
