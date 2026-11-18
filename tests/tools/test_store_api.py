"""Tests for store reads, scoping, and recording-mode writes."""

from __future__ import annotations

import pytest

from ticketideai.schemas.operations import CreateReturn, IssueRefund
from ticketideai.tools.errors import StoreError
from ticketideai.tools.store_api import ChangeRecorder, StoreAPI


@pytest.fixture()
def recording(store_url: str) -> StoreAPI:
    return StoreAPI(store_url, scope_email="customer1@example.com", recorder=ChangeRecorder())


def test_reads_return_seeded_records(store_url: str) -> None:
    api = StoreAPI(store_url)
    assert api.find_customer("  CUSTOMER1@EXAMPLE.COM ").name == "Customer 1"
    assert api.get_order(1).lines[0].sku == "RIDGE-001"
    assert len(api.list_orders("customer1@example.com")) == 3
    assert api.check_stock("RIDGE-037").category == "clearance"


@pytest.mark.parametrize(
    "call",
    [
        lambda api: api.find_customer("missing@example.com"),
        lambda api: api.get_order(9999),
        lambda api: api.check_stock("NO-SUCH-SKU"),
    ],
)
def test_missing_records_raise(store_url: str, call) -> None:
    with pytest.raises(StoreError, match="not found"):
        call(StoreAPI(store_url))


@pytest.mark.parametrize(
    "call",
    [
        lambda api: api.create_return(1, "RIDGE-001", 1, "x"),
        lambda api: api.issue_refund(1, 10.0),
        lambda api: api.restock("RIDGE-001", 1),
        lambda api: api.update_order_status(1, "returned"),
        lambda api: api.send_customer_message("customer1@example.com", "hi"),
    ],
)
def test_writes_are_impossible_outside_recording_mode(store_url: str, call) -> None:
    with pytest.raises(StoreError, match="recording mode"):
        call(StoreAPI(store_url))


def test_scope_hides_other_customers(recording: StoreAPI) -> None:
    for call in (
        lambda: recording.get_order(2),
        lambda: recording.find_customer("customer2@example.com"),
        lambda: recording.list_orders("customer2@example.com"),
        lambda: recording.issue_refund(2, 5.0),
        lambda: recording.send_customer_message("customer2@example.com", "hi"),
    ):
        with pytest.raises(StoreError, match="not found"):
            call()


def test_refund_records_prior_refund_total_as_a_precondition(store_url: str) -> None:
    recorder = ChangeRecorder()
    api = StoreAPI(store_url, scope_email="customer5@example.com", recorder=recorder)
    api.issue_refund(5, 10.0)
    api.issue_refund(5, 20.0)
    first, second = recorder.operations
    assert isinstance(first, IssueRefund) and isinstance(second, IssueRefund)
    assert first.facts.refunded_cents == 23_525
    assert second.facts.refunded_cents == 24_525
    assert first.facts.paid_cents == 47_050


def test_return_records_line_preconditions(recording: StoreAPI) -> None:
    recording.create_return(1, "RIDGE-001", 1, "damaged")
    recording.create_return(1, "RIDGE-001", 1, "damaged")
    first, second = recording._recorder.operations
    assert isinstance(first, CreateReturn) and isinstance(second, CreateReturn)
    assert first.facts.purchased_quantity == 2
    assert first.facts.unit_price_cents == 2_500
    assert (first.facts.already_returned, second.facts.already_returned) == (0, 1)


@pytest.mark.parametrize("amount", [float("nan"), float("inf"), True, "10", None, 10**12])
def test_malformed_refund_amounts_are_rejected(recording: StoreAPI, amount: object) -> None:
    with pytest.raises(StoreError):
        recording.issue_refund(1, amount)


@pytest.mark.parametrize("quantity", [1.5, True, "1", None, 10**9])
def test_malformed_quantities_are_rejected(recording: StoreAPI, quantity: object) -> None:
    with pytest.raises(StoreError):
        recording.restock("RIDGE-001", quantity)


def test_sku_not_on_the_order_is_rejected(recording: StoreAPI) -> None:
    with pytest.raises(StoreError, match="not purchased"):
        recording.create_return(1, "RIDGE-040", 1, "damaged")


def test_recorder_stops_at_its_limit(store_url: str) -> None:
    api = StoreAPI(store_url, recorder=ChangeRecorder(limit=2))
    api.restock("RIDGE-001", 1)
    api.restock("RIDGE-001", 1)
    with pytest.raises(StoreError, match="limit"):
        api.restock("RIDGE-001", 1)
