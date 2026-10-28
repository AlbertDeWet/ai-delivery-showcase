# 03 - Delta pipeline recovery

Contoso Insurance (fictional sample data). Every number below comes from `results/demo_results.json`.

## Problem
A large financial-services client's financial-reporting lakehouse (bronze -> silver -> gold on Delta) hit two incidents:
1. A de-duplication bug doubled some entity-months whenever the source re-delivered a month.
2. A faulty load deleted one month of gold fact rows. It was noticed only days before Delta's vacuum horizon,
   after which time travel can no longer bring the rows back.

This project rebuilds both incidents on synthetic data and runs the recovery end to end on a laptop.

## Approach
- `generate.py`: a deterministic generator. It produces 8 fictional entities in 8 currencies, 75 accounts, 12 months and monthly FX rates, all as Decimal.
- `pipeline.py`: the bronze/silver/gold Delta tables (delta-rs, no Spark), partitioned by month, with a 7-day
  `deletedFileRetentionDuration`. It also builds a **control summary** straight from the source records in
  pure Python Decimal with its own rounding code (`control.py`, not shared with the pipeline). That summary never reads the lake, so it acts as an independent witness.
- `dedupe.py`: finds entity-months where gold holds exactly 2x the control rows and 2x the control amount.
- `dq.py`: checks money columns are DECIMAL and checks for null keys. FX fallback-to-1 is caught three ways: an exact or near-1 rate, an `fx_source` fallback flag, or a missing rate. It also checks per row that `amount_usd = round_half_up(local x rate)` and the rate equals bronze. Then currency sanity (month-on-month jump and median outlier) and row-count lineage on normalised IDs.
- `recover.py`: validates `month` as strict YYYY-MM, identifies the incident explicitly. The operator passes the bad load's commit, which `bad_load` records. Otherwise recover takes the single commit at which the month's row count fell to zero, and refuses if there is more than one candidate (`ABORTED_AMBIGUOUS_INCIDENT`)., and computes the retention window from both `deletedFileRetentionDuration` and `logRetentionDuration`. It then runs the retention check (vacuum horizon and hours left), then a backup with a sha256 manifest. Next it dry-runs V01..V12 on an in-memory candidate. If those pass, it appends the missing month from the pre-incident version via time travel and re-runs the same tests live. Any live failure triggers a **RESTORE to the pre-restore version**, then downstream DQ runs. The restore appends only rows that are not already present (anti-join on normalised txn_id), so a partial reload or a re-run is safe. Each restore commit carries a batch id in its Delta commit metadata. The append-only journal records the intent before the write, then the commit, then each later step, and every run also writes its own evidence file under `runs/`. Any crash from the write onward writes crash evidence. On resume, a batch left inconsistent by a crashed run is rolled back before the retry, but only after a backup, and only when its id is in the tool's own batch registry (`_deltarecover/batch_registry.json` next to the tables) and its commit touched only the target month. A forged tag, a batch that touched other months, an untagged inconsistency, vacuumed pre-incident files, an invalid incident version and a later posting to another month each get their own `ABORTED_*` status, with evidence. A failed rollback also writes evidence (`ROLLBACK_FAILED`). The rollback is refused when another writer has committed after the restore. Last, it writes an evidence pack (`results/recovery_evidence.{md,json}`).
- Amounts are DECIMAL end to end. DuckDB sums DECIMAL exactly, and reconciliation compares Python Decimals to the cent.

Run it: `python -m venv .venv && .venv\Scripts\pip install -r requirements.txt`, then `.venv\Scripts\python run_demo.py`.
Tests: `.venv\Scripts\python run_tests.py` (records both suites in `results/test_runs.txt`).

## How the AI method was applied
This project was built by directing Claude Code agents. An orchestrator set the brief, a builder agent wrote the code and the
builder tests (`tests/`), and an adversarial agent writes its own tests in `tests/adversarial/` and logs
its rounds in `ADVERSARIAL-LOG.md`. The safety pattern in `recover` mirrors the method: backup first, the same tests
before and after, an automatic rollback, and evidence for every claim.

## Results (one recorded demo run)
| Measure | Value |
|---|---|
| Source transactions | 22,459 |
| Doubled entity-months before / after the de-dupe fix (of 96) | 9 / 0 |
| DQ suite, clean run / with one EUR rate removed | PASS / FAIL (fallback-to-1 detected) |
| Rows deleted by the bad load | 1,817 |
| Hours left before the vacuum horizon (simulated clock: found 5d4h after the bad load) | 44.0 |
| Dry-run verify tests passed | 12 / 12 |
| Entity x account cells reconciled to the cent / breaks | 600 / 0 |
| Backup start -> live verify passing | 6.3 s |
| Fault-injected restore (batch written twice in its own commit) | ROLLED_BACK, table matches the pre-restore state |
| Builder tests | 38 passed |
| Adversary tests (rounds 1-5) | 113 passed |

Timings come from one run on a Windows 11 laptop and vary from run to run. See `results/demo_results.md`.

## How this was built
The project was built by directing Claude Code agents. A builder agent (Opus, low effort) wrote the code and the builder tests. An
adversarial agent attacked the work in five rounds, writing its own tests in `tests/adversarial/` (never edited by the builder).
The builder fixed each round until the adversary's tests passed. Where the builder disagreed with a test, it recorded a
"Builder response" for the orchestrator to rule on. The full record, with every finding, fix and ruling, is in `ADVERSARIAL-LOG.md`.

| Round | Adversary test cases | Failed at first run | Findings (Critical / High / Medium / Low) | Closed |
|---|---|---|---|---|
| 1 | 31 | 22 | 13 (1 / 5 / 5 / 2) | 13 |
| 2 | 23 | 11 | 11 (0 / 3 / 6 / 2) | 11 |
| 3 | 21 | 13 | 13 (0 / 1 / 6 / 6) | 13 |
| 4 | 21 | 8 | 7 (0 / 1 / 2 / 4) | 7 |
| 5 | 17 | 3 | 3 (0 / 0 / 0 / 3) | 3 (after the adversary's GO) |
| Total | 113 | | 47 (1 / 10 / 19 / 17) | 47 |

The adversary declared GO in round 5 with 44 of 47 findings closed. The last 3 LOW findings were then fixed, and the latest recorded runs
in `results/test_runs.txt` show 113/113 adversary and 38/38 builder tests passing.

## Limitations
- The clock is simulated (`now` = incident commit + 5d4h). The tests do not actually wait out retention or run VACUUM. A real
  VACUUM that already removed the files is detected and refused.
- Without an explicit incident version, an incident is a commit that empties the month. A bad load that removes only part of the
  month must be named by the operator. When several commits emptied the month, recover refuses until one is named.
- A legitimate later posting to another month that is not in the independent control blocks recovery with
  `ABORTED_UNRECONCILED_LATER_COMMIT` (naming the commit and rows). The control must be refreshed first.
- When other writers have committed after a restore batch, rollback and resume clean-up both refuse, and a person has to merge by hand.
- The batch registry is a local JSON file. A writer that edits it on purpose could still forge a batch, so it is a guard against
  accidents and tag collisions, not against a hostile insider. The guard that actually holds is the touches-only-this-month check.
  A registry that is not a JSON array of objects is refused (`ABORTED_REGISTRY_INVALID`) before anything changes.
- The journal and registry locks are lock files. They are tested with threads and processes on one machine, not on network
  shares or object storage.
- The bad-load delete predicate is still a string, but only after strict ASCII YYYY-MM validation. DuckDB queries use bound parameters,
  with one connection per thread.
- FX fallback detection has no external reference rate source. It uses an exact 1, a fallback flag, a missing rate, a near-1 rate
  (within 0.1%) for any currency that is not in the configured `NEAR_PARITY_ALLOWLIST` (named per month), a currency at near-1 in every
  month, and a 5% band around the currency's own median. The median can be poisoned if most months are wrong, so the near-1 rules
  name the bad months directly. A currency that really trades at parity must be added to the allowlist.
- IDs differing only by case or whitespace are merged. Any normalised ID with differing content (including exact duplicates and
  NULL fields) is reported as an `id_collision`, but the pipeline still merges them.
- Small scale (about 22k rows, 600 cells). Performance at production scale has not been measured. Timings vary a lot between runs on a busy laptop.
