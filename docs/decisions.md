# Architecture Decisions

Decisions are grouped by topic.

## Execution safety

### ADR-001: Policy evaluates effects, not source code

**Decision:** The policy engine reads the recorded change set and never inspects plan source.

**Why:** Source analysis cannot see computed amounts or loops. Three refunds of $150 in a loop look harmless in the source and total $450 in effect.

### ADR-002: Commit applies recorded operations

**Decision:** Commit applies typed operations in trusted code after comparing each recorded fact with the store. Plans are never run again.

**Why:** Running model code again could produce different effects from the ones that were reviewed. A stale fact aborts the whole transaction and triggers a new plan.

### ADR-003: The child's report is verified before use

**Decision:** The parent replays the reported change set on an untouched database copy and builds the diff from that replay.

**Why:** The child runs untrusted code, so its output cannot be trusted. Forged facts fail this replay.

### ADR-004: Commit takes the write lock before reading

**Decision:** The store is file-backed SQLite, and commit uses `BEGIN IMMEDIATE`.

**Why:** Holding the lock from the first read closes the gap between checking preconditions and writing, and SQLite keeps the project self-contained.

### ADR-005: Busy databases and changed state lead to a new plan

**Decision:** A precondition failure or database operational error rolls the transaction back and returns the request to the planner with current store state.

**Why:** Both conditions are usually temporary, and a new plan keeps every check in force. When corrections run out, the request ends as `failed_safely`.

## Sandbox

### ADR-006: The sandbox refuses to run as root

**Decision:** The sandbox raises a configuration error when its effective user is root, in both the parent and the child. On Linux, plan code also will not start unless the CPU, address-space, file-size, open-file, core-dump, and process limits were all applied.

**Why:** Linux exempts root from `RLIMIT_NPROC`, so the "no child processes" limit would silently stop working. Failing closed is safer than running with a weaker sandbox.

### ADR-007: Layered controls instead of one boundary

**Decision:** Plans pass an AST allowlist, then run in an isolated child with kernel limits, an audit hook, and limits enforced by the parent.

**Why:** A Python-level allowlist alone is not a security boundary. Each layer still holds when another one is bypassed, and tests exercise the runtime layers with the validator skipped.

Memory uses a different hard limit on each platform: the kernel's address-space limit on Linux, and the kernel's peak-memory record on macOS. Linux carries a parent's peak over `exec`, so that record cannot be used there.

## Store policy

### ADR-008: Refund totals are cumulative

**Decision:** The approval threshold applies to refunds on an order including earlier refunds, and to the total refunded by one plan across orders.

**Why:** Otherwise a large refund could be split across requests or orders.

### ADR-009: Rules that close common bypasses

**Decision:** Policy also enforces these rules:

| Rule | Action | Closes |
|---|---|---|
| Orders and messages must belong to the requester | Block | Acting on another customer's account |
| Refunds above the value of returned items need review | Escalate | Refunds with no returned goods |
| Restocks must match units returned in the same plan | Block | Inflating inventory from a chat message |
| Only delivered orders can be returned | Block | Returning goods that never arrived |
| Support may set only `cancelled` and `returned` | Block | Marking an order delivered to unlock a return |

### ADR-010: No restocking fee

**Decision:** Refunds use the full unit price, and the restocking rule is the return-backed restock above.

**Why:** A fee setting that nothing applies would imply behavior the store does not have.

### ADR-011: Store date is injectable

**Decision:** Policy receives the current time from its caller, and `STORE_AS_OF` can pin it for the seeded demo store.

**Why:** Seed data has fixed dates, so return-window results would otherwise change as real time passes.

## Human review

### ADR-012: Approval is a human-only channel

**Decision:** Only the staff CLI and Streamlit panel can approve a change set. Plans have no approval operation, and each approval records a named reviewer.

**Why:** A model that can approve its own proposals makes the review meaningless.

### ADR-013: Staff decisions notify the customer from recorded facts

**Decision:** Approve, reject, and stale decisions write a template notice in the same transaction as the decision. An approval notice lists only the operations in the committed change set.

**Why:** The customer learns the real result, and no model can misstate it.

## Agent behavior

### ADR-014: Outcomes separate refusals from safety failures

**Decision:** Lookups end as `answered`. Policy and planner refusals end as `refused`. Requests that run out of corrections end as `failed_safely`.

**Why:** A store policy decision and a system that could not finish safely mean different things to a customer and to anyone reading the metrics.

### ADR-015: Policy blocks are final

**Decision:** Reflection handles invalid plans, failed runs, and changed state. A policy block is never sent back to the planner.

**Why:** Retrying after a block would teach the model to search for a way around policy.

### ADR-016: Planner context and API results have different shapes

**Decision:** The planner prompt states that API results are objects read with dots, says `store_context` is not available to the plan, and includes a working example that a test executes.

**Why:** Plans that indexed an order, as in `order['status']`, failed with `'Order' object is not subscriptable`. The planner reads orders as JSON in `store_context`, while the store API returns objects. The validator and sandbox were correct; the prompt had not stated the difference.

### ADR-017: The reply guard is a deterministic backstop

**Decision:** Code checks each reply for completion claims that the outcome does not support and for unsupported amounts. It understands negated claims, and it accepts an amount quoted from the customer's message only when no money is moving and the sentence claims nothing was completed.

**Why:** The responder's prompt lowers the error rate, but a claim that a refund is done while it is pending must never reach a customer.

## Evaluation

### ADR-018: Outcome and state are scored separately from policy

**Decision:** A run succeeds only when the outcome and the final store both match the scenario. An auditor reads before and after snapshots to count policy violations without using the policy engine.

**Why:** A correct-looking outcome can hide a wrong refund amount, and a checker that reuses the policy engine could not detect a defect in that engine.

### ADR-019: Evaluations use scratch databases

**Decision:** Every evaluation run creates its own seeded database and never reads the configured store.

**Why:** Runs stay independent and repeatable, and an evaluation cannot alter real data.

### ADR-020: Only application spans are exported to Langfuse

**Decision:** The Langfuse client exports spans created through its own SDK and drops spans emitted by provider SDKs.

**Why:** The Anthropic SDK emits its own span for each API call, and Langfuse prices that span as well as the generation recorded by `LLMProvider.complete`. Exporting one record per call keeps trace totals correct.
