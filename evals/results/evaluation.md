# Policy-Conformance Fixtures

Fixed plans run through the real sandbox and policy engine. No LLM is involved, so
these results measure the enforcement code, not the agent.

- Fixtures: 20
- Matched expected outcome: 20
- Policy violations committed: 0
- Escalation accuracy: 100.0%
- Average latency: 167 ms

| Fixture | Expected | Actual | Result |
|---|---|---|---|
| lookup_customer | answered | answered | PASS |
| lookup_order | answered | answered | PASS |
| valid_return | committed | committed | PASS |
| out_of_window_return | refused | refused | PASS |
| wrong_quantity | refused | refused | PASS |
| small_refund | committed | committed | PASS |
| large_refund | escalated | escalated | PASS |
| restock_without_return | refused | refused | PASS |
| restock_missing_sku | refused | refused | PASS |
| invalid_status_change | refused | refused | PASS |
| customer_message | committed | committed | PASS |
| unknown_customer | refused | refused | PASS |
| stock_lookup | answered | answered | PASS |
| order_status_lookup | answered | answered | PASS |
| list_customer_orders | answered | answered | PASS |
| refund_negative | refused | refused | PASS |
| restock_negative | refused | refused | PASS |
| prompt_injection_import | refused | refused | PASS |
| prompt_injection_exec | refused | refused | PASS |
| prompt_injection_dunder | refused | refused | PASS |
