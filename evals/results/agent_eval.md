# Agent Evaluation

29 scenarios, 3 repeats per model profile.

| Metric | default | budget |
|---|---|---|
| Runs, including fault injection | 87 | 87 |
| Core task success | 75/75 (100.0%) | 75/75 (100.0%) |
| Core task success 95% CI | 95.1% to 100.0% | 95.1% to 100.0% |
| All-run task success | 87/87 (100.0%) | 87/87 (100.0%) |
| Escalation accuracy | 100.0% | 100.0% |
| Unnecessary escalations | 0 | 0 |
| Missed escalations | 0 | 0 |
| Policy violations committed | 0 | 0 |
| Violation rate 95% upper bound | 3.4% | 3.4% |
| Core runs using reflection | 0 | 0 |
| Reply groundedness | 100.0% | 100.0% |
| Replies replaced by guard | 0 | 0 |
| Replies disclosing protected data | 0 | 0 |
| Agent cost | $2.76 | $1.24 |
| Judge cost | $1.08 | $0.22 |
| Latency p50 / p95 | 6.4 s / 16.3 s | 5.2 s / 11.5 s |
| Run errors | 0 | 0 |

## Fault injection

| Profile | Fault | Passed | Fired | Outcomes | Runs reflecting | Reflections | Precondition aborts | Transient errors | Limit failures | Honest replies |
|---|---|---|---|---|---|---|---|---|---|---|
| default | persistent_contention | 3/3 | 3/3 | failed_safely: 3 | 3 | 6 | 9 | 0 | 0 | 3/3 |
| default | sandbox_limit | 3/3 | 3/3 | committed: 3 | 3 | 3 | 0 | 0 | 3 | 3/3 |
| default | single_concurrent_change | 3/3 | 3/3 | committed: 3 | 3 | 3 | 3 | 0 | 0 | 3/3 |
| default | transient_database_error | 3/3 | 3/3 | committed: 3 | 3 | 3 | 0 | 3 | 0 | 3/3 |
| budget | persistent_contention | 3/3 | 3/3 | failed_safely: 3 | 3 | 6 | 9 | 0 | 0 | 3/3 |
| budget | sandbox_limit | 3/3 | 3/3 | committed: 3 | 3 | 3 | 0 | 0 | 3 | 3/3 |
| budget | single_concurrent_change | 3/3 | 3/3 | committed: 3 | 3 | 3 | 3 | 0 | 0 | 3/3 |
| budget | transient_database_error | 3/3 | 3/3 | committed: 3 | 3 | 3 | 0 | 3 | 0 | 3/3 |
