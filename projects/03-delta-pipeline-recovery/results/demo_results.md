# Demo results

Contoso Insurance (fictional sample data). Produced by `run_demo.py`.

| Measure | Value |
|---|---|
| Source transactions generated | 22459 |
| Entity-months checked (incident A) | 96 |
| Doubled entity-months before fix | 9 |
| Doubled entity-months after fix | 0 |
| DQ suite on clean run | PASS |
| DQ suite with one missing FX rate | FAIL (detected) |
| Rows deleted by bad load (incident B) | 1817 |
| Hours left before vacuum horizon (simulated clock) | 44.0 |
| Dry-run verify tests passed | 12/12 |
| Rows restored via time travel | 1817 |
| Entity x account cells reconciled / breaks | 600 / 0 |
| Backup -> live verify passing (s) | 6.306 |
| Recovery status | RECOVERED |
| Fault-injected run status | ROLLED_BACK |

## Timings (s)

| Step | Seconds |
|---|---|
| generate | 0.44 |
| pipeline_buggy | 8.914 |
| pipeline_fixed | 3.794 |
| dq_clean | 2.675 |
| dq_missing_fx | 3.601 |
| recover | 10.713 |
| recover_with_fault | 6.005 |

Python 3.13.5 on Windows-11-10.0.26100-SP0.
