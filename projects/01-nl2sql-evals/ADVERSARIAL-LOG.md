# Adversarial log: 01-nl2sql-evals

Role: ADVERSARY. The adversary tries to break the work and writes its own tests in `tests/adversarial/`
(every test docstring starts with "ADVERSARY:"). It may write only there and in this file. No live model
calls were made in any round: every provider in the adversary's tests is a fake runner or a scripted provider.

## Round 0 (history)

Two independent reviews came before this log. Neither was written by the adversary role.

**Review 1: first build (offline baseline only).** The review report itself is not kept in the repo. The
README says it found 15 issues and names four of them: a grader that tolerated off-by-one counts, a LIMIT
regex that broke on trailing comments, `SHOW`/`SUMMARIZE`/`DESCRIBE` passing the guard, and traps that did not
bite. CHANGES 1-12 list the fixes:

1. Exact comparison for integral values (counts, years).
2. claude-cli parsing: malformed output becomes an error result.
3. LIMIT by wrapping (`SELECT * FROM (<sql>) LIMIT n`), not by regex.
4. API key, auth token, base URL and `CLAUDE_CODE_*` stripped from the child environment.
5. Repo-local noreply commit identity and a denylist check in the hooks.
6. Single-pass tokenizer for strings and comments.
7. SHOW, SUMMARIZE and DESCRIBE rejected.
8. Executor memory and thread limits.
9. Fiscal-year boundary rows planted, plus mutant tests.
10. j04 and s08 reworked.
11. Held-out set of 15 questions added.
12. README honesty fixes.

Twelve fix items are listed for 15 issues. The log cannot check how the other three were handled, because the
review text is not in the repo.

**Review 2: live Haiku and Sonnet evals (`results/REVIEW-live-evals.md`).** Verdict: PASS WITH CONCERNS.
It recomputed every summary metric with zero differences and found no gold SQL in any prompt. Its numbered
Findings section has 10 items: four "fix now", three "ask why" and three "leave it". With the s03/m10
timeout analysis and the definitions it asked the README to state, the brief counts 12. The main items were:

- Opus shown as 0.0% in report.md.
- The README Results section was stale.
- The j03 gold defect (NULL vs 'Unknown').
- The cost column excluded checker calls.
- h15 is a judgment call.
- Haiku costing more than Sonnet.
- Latencies above the timeout.
- The Sonnet s03/m10 CLI timeouts (infrastructure).
- Metric definitions that were not obvious.

CHANGES 13-19 list the fixes:

- 13: all-error runs render as "not run".
- 14: the j03 question now names the 'Unknown' label, and j03 was re-run.
- 15: infra-only retry rule, with s03/m10 re-run.
- 16: h15 documented.
- 17: cost, quantile and mean/max labels.
- 18: Results rewritten with the schema-card caveat.
- 19: `eval --label` writes to `reruns/`, and `merge` writes to `derived/`.

## Round 1 (adversary attack, 2026-10-23)

Suite: `tests/adversarial/`, 58 tests, about 9 s. **36 PASS, 22 FAIL.** The existing suite is unchanged:
240 passed. Most attacks rely on the second layer. The executor
(`read_only`, `enable_external_access=false`) stopped every file-access attempt that got through the guard.

### Findings

| # | Sev | Area | Finding | Repro | Test | Status |
|---|---|---|---|---|---|---|
| A1 | High | merge | **Cherry-pick by repetition.** `save()` writes `reruns/<provider>-<tier>-<label>.json` and silently overwrites it. You can re-run the same failing case until it passes, and only the lucky file survives. `merge` keeps no count of attempts. Nothing requires the re-run cases to be chosen without looking at the outcome: only the failures were re-run, never a sample of the passes. | `save()` twice with the same label gives the same path | `test_adv_merge::test_saving_a_rerun_never_overwrites_earlier_rerun_evidence` | FAIL |
| A2 | High | merge | A re-run that claims `repeats=3` but holds **one** passing record per case is accepted, so pass^k is judged on 1 attempt. Only the `repeats` field is compared, not the records. | `merge_results(base, [run([rec('s03',True)], repeats=3)])` | `test_rerun_must_have_repeats_records_per_case` | FAIL |
| A3 | Medium | merge | A re-run may **add case ids** that are not in the base. They are appended, which inflates n and accuracy. | re-run with id `zz9` | `test_rerun_cannot_add_case_ids_absent_from_base` | FAIL |
| A4 | Low | merge | The model id is not checked. Only provider, tier and suite are. | re-run with model `claude-sonnet-5-5` on a haiku base | `test_rerun_with_a_different_model_is_rejected` | FAIL |
| A5 | Low | merge | Derived files have **no top-level `retry_rule`**. They copy the pre-rule base, but their re-run records ran under the rule. The mix is visible only inside `derived_from`. | inspect `results/derived/*.json` | `test_derived_result_states_the_retry_rule_it_mixes` | FAIL |
| G1 | Medium | guard | **CTE scope bypass.** `_walk` collects CTE names globally, so a CTE named `"f.csv"` inside one subquery whitelists `'f.csv'` as a file table in the outer query. The executor stops it: "file system operations are disabled". The guard layer is defeated, and the second layer holds. | `SELECT * FROM 'f.csv', (WITH "f.csv" AS (SELECT 1) SELECT * FROM "f.csv") t` | `test_cte_in_subquery_does_not_whitelist_a_file_path_elsewhere` (FAIL); `test_executor_still_stops_the_cte_scope_bypass` (PASS) | FAIL |
| G2 | Low | guard | Scalar config introspection passes the guard and runs: `current_setting('memory_limit')` returns `953.6 MiB`. `getvariable()` passes too. | as stated | `test_scalar_config_introspection_is_blocked` (x2) | FAIL |
| G3 | Low | guard | **False block.** The keyword scan reads inside quoted identifiers, so `AS "policies in use"` and `AS "Show total"` are blocked. A correct answer is then graded as wrong. | as stated | `test_english_words_in_quoted_alias_are_not_false_blocked` (x2) | FAIL |
| G4 | none | guard | Held: `$$`/`$tag$` strings, non-nesting `/* /* */` comments, `E'\''` and plain `'a\'` all tokenise the same way DuckDB parses them. Quoted file paths, s3/https URLs, `json_execute_serialized_sql`, quoted `"read_text"`, `read_csv /*x*/ (`, `duckdb_settings()`, `duckdb_tables`, `information_schema`, LATERAL `generate_series` and PIVOT are all blocked. | | `test_quoted_paths_and_table_functions_blocked` (13), `test_tokenizer_agrees_...` (5), `test_nested_comment_close_...` | PASS |
| R1 | Medium | grader | **Integer exactness bypass.** The exact rule applies only when *both* values are integral. Gold count 1000 against a predicted 1000.4 passes. | `result_sets_match([(1000,)], [(1000.4,)])` returns True | `test_integer_gold_is_exact_even_when_prediction_is_fractional` | FAIL |
| R2 | Medium | grader | **Relative tolerance on large totals.** 1e-3 of the 7.39M GWP is about 7.4k. A query that drops a real premium row (82.88) grades as correct. Fixed mutants pass, but missing rows of moderate size do not register. | GWP with the smallest premium row excluded | `test_dropping_a_premium_row_from_gwp_fails` | FAIL |
| R3 | Medium | grader | **Shotgun answers.** A single row with 8 candidate numbers passes if any column matches gold, because extra columns are projected away. A model can hedge. The README admits shape leniency, but not this degree of it. | `[(1206,)]` vs `[(1200,…,1207)]` | `test_shotgun_wide_prediction_does_not_pass` | FAIL |
| R4 | Low | grader | NaN is normalised to None, so a NaN answer matches a NULL gold. | | `test_nan_is_not_null` | FAIL |
| E1 | Medium | report | The **all-error guard hides model failures.** A run where every record is an *execution error* (the model's SQL fails in DuckDB), or an *unparseable answer* that consumed tokens, is rendered as "not run: ...". It should be a 0% model result. The guard tests `action == "error"` and ignores whether the error was infrastructure. | runs of token-consuming error records | `test_model_execution_errors_are_not_hidden_as_not_run`, `test_unparseable_model_output_is_not_hidden_as_not_run` | FAIL |
| E2 | Low | report | The reverse case: 20 infrastructure errors and 1 success render as a plain 4.8% result with no infrastructure warning. | | `test_nearly_all_infra_error_run_is_flagged` | FAIL |
| I1 | Medium | retry | **The retry can hide a model error.** "Infra" is decided only by a token count of 0. A CLI envelope with no `usage` block makes a non-JSON (unparseable) model answer count as 0 tokens, so it is retried. The README says unparseable answers are never retried. | envelope `{"is_error":false,"result":"I think the answer is 42"}` triggers 2 calls | `test_unparseable_answer_without_usage_block_is_not_retried` | FAIL |
| I2 | Low | retry | An `is_error` envelope with no usage, such as "API Error: 400 model not supported", is a configuration error, but it is retried as infrastructure. | | `test_cli_reported_model_error_without_tokens_is_not_retried` | FAIL |
| I3 | none | retry | Held: an execution error is never retried, and a retry leaves `retries=1` and `first_error` in the record. | | 2 tests | PASS |
| D1 | Low | README | Finding 3 says "against 0.14 s for the offline baseline". The recorded baseline p50 is 0.123 s. That figure is stale from the pre-j03-fix baseline run. | | `test_baseline_latency_in_findings_matches_recorded_run` | FAIL |
| D2 | Low | README | The pre-fix baseline figures, 77.8% / 73.8%, are quoted but are not in any `results/*.json`. The run was overwritten. Only `REVIEW-live-evals.md` attests to them, which breaks the brief's rule 4 in letter. | | `test_pre_fix_baseline_numbers_are_backed_by_a_results_file` | FAIL |
| D3 | Low | README | The README says runs recorded before the rule "say `retry_rule: none`". `claude-cli-haiku.json` and `claude-cli-sonnet.json` have **no** `retry_rule` key. The report supplies "none" as a default. | | `test_runs_recorded_before_the_retry_rule_say_none` | FAIL |
| D4 | none | README | Held: every number in both README results tables matches its named file (10 files), and the "23 to 28 s" re-run latency claim holds. `report.md` regenerates byte-identically. Every stored summary recomputes with zero metric differences. | | 11 tests + manual check | PASS |

### Verdict: NO-GO (for this round)

The headline numbers are honest. They match the files, and the re-run cases did pass. The guard leaks once,
but the executor contains the leak. The blockers are about **trust in the tooling the showcase advertises**:

- **A1 and A2:** the derived-results path can be cherry-picked, by overwriting re-run files or by
  under-filled re-runs.
- **R1, R2 and R3:** the grader can accept wrong answers that the README says it rejects. Counts are said
  to be exact, but R1 shows they are not.
- **E1 and I1:** the all-error guard and the retry rule can each turn a model failure into "not run" or a
  silent second attempt. That is the opposite of what the README claims.

The verdict changes to GO when A1, A2, A3, R1, E1 and I1 are fixed and their adversarial tests pass. R2 and R3
also need either a fix or an explicit README limitation that names them. G1 should be fixed in the guard,
even though the executor contains it. The Low items can be closed by fixing them or by documenting them.

## Round 1 fixes (builder, 2026-10-23)

Builder role. No file in `tests/adversarial/` was edited, and no adversary row above was changed.
No live model calls were made in this round. Every number change was produced by offline
re-execution of stored SQL.

**Result:** adversary suite **57 pass, 1 fail** (was 36 pass, 22 fail). Full suite **318 pass, 1 fail**
(319 tests: 240 existing, 58 adversarial, 17 new in `tests/test_round1.py`, and 4 net new in
`tests/test_grader.py`). The remaining failure is D3; see the Builder response.

| # | Fix | Where | Adversary test |
|---|---|---|---|
| A1 | Re-runs are saved as numbered attempts (`<series>-attemptN.json`). Overwrite raises. A legacy unnumbered file counts as attempt 1. `merge` counts the attempts on disk (by content), refuses if any is not supplied, uses the **earliest** attempt per case, and records each attempt's score in `derived_from.series`. | `evals.save`, `rerun_attempts`, `merge_results`, `cli merge` | PASS |
| A2 | Each re-run case must have exactly `repeats` records with repeat indices 0..k-1. | `_validate_rerun` | PASS |
| A3 | Re-run case ids must be a subset of the base run's ids. | `_validate_rerun` | PASS |
| A4 | The model id must match. | `_validate_rerun` | PASS |
| A5 | Derived files carry a top-level `retry_rule`, for example `mixed: base records: none; re-run records: infra-only ...`. | `merge_results` | PASS |
| G1 | The AST walk scopes CTE names to the defining query node and its subtree. CTE names must be plain identifiers. | `guard._walk` | PASS |
| G2 | `current_setting(` and `getvariable(` added to the forbidden functions. | `guard.FORBIDDEN_FUNCS` | PASS |
| G3 | The keyword scan reads code tokens only. The function scan still reads quoted identifiers (`"read_csv"(` and `"current_setting"(` stay blocked). | `guard.check` | PASS |
| R1 | Integral gold requires an integral-valued, exactly equal prediction. | `grader.numbers_equal` | PASS |
| R2 | No relative tolerance. Non-integral gold is compared at its own precision: half a unit in the last place, at least 2 dp at or above 1 (±0.005) and at least 4 dp below 1. | `grader.numbers_equal` | PASS |
| R3 | A wider one-row prediction must align with the gold columns: first columns, last columns, or by column name. Multi-row answers keep subset projection. | `grader.result_sets_match` | PASS |
| R4 | NaN uses a sentinel and equals only NaN. | `grader._norm` | PASS |
| E1 | "Not run" only if every record is a zero-token error with no model output (transport failure, CLI-reported API error, failed process). Token-consuming execution errors and unparseable answers render as results (0%). | `evals.is_all_error`, `pipeline.is_no_output_record` | PASS |
| E2 | Rows with any infrastructure errors are flagged, for example `96.9% (incl. 2/162 infra errors)` on the recorded Sonnet main run. | `evals._summary_row` | PASS |
| I1 | A retry needs an explicit transport failure (`infra=True` or the `claude CLI failed:` prefix, which the provider emits only for a timeout or OSError) **and** 0 tokens. A missing usage block no longer qualifies. | `pipeline.is_infra_error`, `claude_cli._call` | PASS |
| I2 | An `is_error` envelope is not infra, so it is not retried. | as I1 | PASS |
| D1 | Findings now say 0.12 s, which is `round(0.123, 2)` from `baseline-rules-v1.json`. | README | PASS |
| D2 | `results/history/baseline-rules-v1-questions-pre-j03-fix.json` reproduces 77.8% / 73.8%: a deterministic baseline run against `evals/history/questions-pre-j03-fix.yaml` (identical except the old j03), made **before** the grader changed. The README cites it. | results/history | PASS |
| D3 | README wording fixed: the old live runs "have no `retry_rule` key at all", and the report, regraded and derived files treat it as `none`. The recorded files were **not** edited. | README | FAIL (see Builder response) |

**Offline regrade (grader v2, no model calls).** `python -m nl2sql regrade` re-executes each
stored SQL through the current guard and executor and grades it against the eval set the run
was recorded with. The live main runs used the pre-j03-fix history set. It covered 9 files (6
recorded full runs and 3 re-runs, written to `results/regraded/`) and 384 SQL records. **0
records changed. No reported number changed**, and the 4 merges rebuilt on regraded inputs
(`results/regraded/derived/`) equal the originals. Control: altering one stored query
(`COUNT(*) + 1`) flips exactly that record.

**Number changes this round:** none to any reported accuracy figure. Two README text figures
were corrected: baseline latency 0.14 s became 0.12 s (D1), and the 77.8% / 73.8% figures now
cite a results file (D2). The recorded Sonnet main row in `report.md` now carries
"(incl. 2/162 infra errors)".

### Builder response (disagreements and judgment calls)

- **D3, not fixed in the files, deliberately.** The test asserts that `claude-cli-haiku.json` and
  `claude-cli-sonnet.json` contain `"retry_rule": "none"`. Those are recorded evidence. Adding a
  key after the fact would make them say something they did not say when written, and the
  live-eval review's rule is to not hand-edit stored files. The coordinator asked for the README
  wording to be fixed, and it now describes the files accurately. Derived and regraded files
  carry an explicit `retry_rule`. I propose the test be changed to assert the README wording, or
  `get("retry_rule", "none") == "none"`.
- **E1 vs the Opus case.** "Not run" is wider than strictly 0-token transport errors: it also
  covers a run where every record is a zero-token CLI-reported API error or a failed process. In
  that case no model produced any output, as with the Opus "version 2.1.280 required" run. The
  retry rule stays strict (transport only), so I2 holds. Without this, an all-400 Opus run would
  render as 0.0%, which is the misleading display review 2 flagged. All four E-tests pass.
- **R1 strictness.** Integral gold now rejects any non-integral prediction, including whole money
  amounts such as 5000.00 against an unrounded 5000.004. This follows the coordinator's rule.
  The regrade shows that no recorded answer depended on it.
- **R2 precision rule.** A 3-dp gold against a 2-dp prediction now fails, so I changed one
  assertion in the builder's own `test_grader.py`, which had asserted the opposite. A
  1.2345-vs-1.2399 ratio now fails, where the round-0 rule would have passed it. Documented in
  the README limitations.
- **R3.** Left- or right-aligned plus by-name still allows a hedge of two (first and last
  column). This is documented in the README limitations, not eliminated.
- **I1, non-zero exit without output** (for example "not logged in") is now *not* retried, as a
  strict reading of "only an explicit CLI timeout or transport error". It still renders as "not
  run" if every record is like that.
- **Builder test changes** (none weaken a check): `test_grader.py` has two tests rewritten to the
  v2 rule, plus four new ones. In `test_reporting.py`, the Opus fixture message now uses the exact
  `CLI reported error: ...` form that the provider emits (copied from
  `results/failed-runs/`).

## Round 2 (adversary attack on the Round 1 fixes, 2026-10-23)

**Status at the start of the round.** The builder fixed the Round 1 findings, and 57 of the 58 Round 1 tests
passed. The one remaining failure was D3. The orchestrator ruled on it: recorded result files are never
edited after the fact. The adversary therefore rewrote that test as
`test_adv_readme::test_readme_states_pre_rule_runs_have_no_retry_rule_key`. The new test checks two things:

- the pre-rule files still have no `retry_rule` key;
- the README says so honestly, and no longer claims those files "say `retry_rule: none`".

The rewritten test now passes.

**New attacks.** They are in `tests/adversarial/test_adv_round2.py`. The adversarial suite now has
**81 tests: 75 PASS and 6 FAIL**, and runs in about 6 s. The full suite has **342 tests: 336 PASS and 6 FAIL**,
and the 6 failures are the same 6 adversarial tests. No live calls were made.

| # | Sev | Area | Finding | Test | Status |
|---|---|---|---|---|---|
| A6 | Medium | attempts | **Copy-elsewhere bypass.** The CLI counts attempts only in the folder of the file you pass. You can copy the lucky `attempt2` to another folder and pass the copy. Merge then sees 1 attempt on disk, accepts it, and never counts the failed `attempt1`. No file is edited. | `test_copied_attempt_elsewhere_cannot_hide_siblings` | FAIL |
| A7 | Medium | attempts | **Deletion leaves a gap that is not checked.** If you delete `attempt1` (the failure) and merge `attempt2`, merge accepts it, although attempt numbering that starts at 2 proves an attempt is missing. | `test_deleted_attempt_leaves_a_detectable_gap` | FAIL |
| A8 | Low | attempts | **Relabelling.** If you edit `label` in a JSON file, that attempt becomes its own one-attempt series. This is deliberate falsification of evidence, so the real control is git history. It is listed so the README does not overclaim. | `test_relabelled_attempt_cannot_escape_its_series` | FAIL |
| R5 | Medium | grader | **Multi-row hedge.** The R3 fix covers one-row answers only. A per-group answer that carries 3 candidate measure columns still passes, because any column subset is searched when there is more than one row. | `test_multi_row_hedge_columns_fail` | FAIL |
| R6 | Low | grader | **Two-candidate hedge on one-row answers.** Gold is accepted at the first or the last column, so `(1205, 1206)` passes against gold 1206. The by-name path also uses alias names the model chose. | `test_one_row_two_candidate_hedge_fails` | FAIL |
| R7 | Low | grader | **False negative from float noise.** When the gold is integral, the prediction must be an exact integer. So `1000.0000000001`, which a DOUBLE sum in a different order can produce, is graded wrong. The offline regrade changed no numbers, so this is latent. | `test_integral_gold_tolerates_float_summation_noise` | FAIL |
| - | none | grader | Held: a wrong middle column is not skipped, and with gold left-aligned a wrong trailing column is ignored. Gold values with trailing zeros are read at the right precision: `0.1` is treated as a ratio at ±0.00005, `12.5` as money at ±0.005, and `45.0` as an integer, so `45.004` fails. | 5 tests | PASS |
| - | none | attempts | Held: attempts are numbered and never overwritten. Merge refuses when a sibling attempt is left out of the same folder. | 2 tests | PASS |
| - | none | not run | Held: a zero-token execution error is still a result, while zero-token CLI timeouts alone render as "not run". The Round 1 E1 and E2 tests pass. | 2 tests | PASS |
| - | none | guard CTE | Held: CTE names no longer leak out of their scope (sibling, UNION branch, scalar subquery), and a CTE named like a file path is refused. Nested, recursive and MATERIALIZED CTEs still pass. | 8 tests | PASS |

### Verdict: GO

No Critical or High findings are open. Every Round 1 High finding (A1 and A2) and every Round 1 blocker
(A3, R1, E1, I1 and G1) has a passing adversary test.

The 6 open findings are Medium or Low. Each needs a fix or an honest README limitation:

- **A6:** count attempts across `results/reruns/` whatever path is passed, or refuse paths outside it.
- **A7:** refuse gaps in attempt numbers.
- **A8:** state in the README that git history is the tamper control.
- **R5:** limit the subset search for multi-row answers, or name the hedge in the README.
- **R6:** document it.
- **R7:** accept integral gold within 1e-6.

## Final state

**Round 0 (history).**

- **Review 1** of the first build found 15 issues. CHANGES 1-12 cover the fixes.
- **Review 2** of the live Haiku and Sonnet evals (`results/REVIEW-live-evals.md`) was PASS WITH CONCERNS:
  - it recomputed every metric with zero differences and found no gold leakage;
  - it led to fixes for the Opus 0% display, the j03 gold defect, the cost label, the h15 judgment call,
    the infra-only retry rule and the targeted re-run tooling (CHANGES 13-19).

**Round 1.** The adversary wrote 58 tests, and 22 failed first.

| Area | IDs | Finding |
|---|---|---|
| merge | A1 (High), A2 (High), A3, A4, A5 | cherry-picking of re-runs and under-filled re-runs |
| guard | G1, G2, G3 | CTE scope bypass, config introspection, false blocks |
| grader | R1, R2, R3, R4 | integer bypass, tolerance on large totals, shotgun answers, NaN |
| report | E1, E2 | "not run" hiding model failures |
| retry | I1, I2 | retry of unparseable answers and API errors |
| README | D1, D2, D3 | stale or unbacked numbers |

The verdict was **NO-GO**. The builder fixed these, and 57 of the 58 tests passed. The orchestrator ruled
on D3 that recorded files stay unedited, and the adversary rewrote that test against the README instead.

**Round 2.** All 58 Round 1 tests pass, including the rewritten D3 test. The adversary added 23 tests on the
fixes, and 6 of them fail:

- Medium: A6, A7 and R5;
- Low: A8, R6 and R7.

**Final.** The adversarial suite has 81 tests: 75 PASS and 6 FAIL. The full suite has 342 tests: 336 PASS
and 6 FAIL, and all 6 failures are adversary tests for open Medium or Low findings. No Critical or High
finding is open. **GO**, with the six items above carried as follow-ups.

## Round 2 fixes (builder, 2026-10-23)

Builder role. No file in `tests/adversarial/` was edited, and no adversary row was changed. No live
model calls were made.

**Result:** adversary suite **81 / 81 pass** (was 75 / 81). Full suite **351 / 351 pass**: 342
existing, plus 9 new in `tests/test_round2.py`. CI is green.

| # | Fix | Where | Adversary test |
|---|---|---|---|
| A6 | Merge counts every stored re-run across **all** of `<results-dir>/reruns/` (recursive) plus the supplied folders, whatever path is passed. Matching is by content (provider, tier, suite, model, label, attempt). | `evals.check_rerun_completeness`, `cli merge` | PASS |
| A7 | A gap in a series' attempt numbers (for example only attempt 2 present) is refused. | same | PASS |
| A8 | Cheap partial fix: any stored re-run that covers a merged case id must be supplied, **whatever its label**. A relabelled copy no longer hides the original attempt. Residual: deleting the original *and* relabelling leaves nothing on disk to detect. The README Limitations section now says that git history is the tamper control. | same, README | PASS |
| R5 | Multi-row hedges are rejected: an extra numeric column within 10% of the matched gold value on every row is a second candidate. See the Builder response for why the positional-only rule was not applied to multi-row answers. | `grader._is_hedge` | PASS |
| R6 | The same hedge rule applies to one-row answers, so `(1205, 1206)` fails. The residual heuristic limit is documented in the README Limitations. | `grader._is_hedge`, README | PASS |
| R7 | Integral gold is compared within 1e-6, which absorbs float summation noise. 1000 vs 1000.4 still fails. | `grader.numbers_equal` | PASS |

**Offline regrade, re-run** under the round 2 grader (`GRADER_VERSION = "v2-round2"`) on all 9
recorded files. **0 records changed. No reported number moved.** The 4 merges rebuilt on regraded
inputs pass the new completeness checks and equal the originals: Haiku j03-fix 100 / 100 / 100;
Sonnet j03-fix 98.8 / 98.4 / 96.3; Sonnet infra-retry 98.1 / 97.6 / 98.1; Sonnet both
100 / 100 / 100.

### Builder response

- **R5: the literal "positional or by name" rule was tried and rejected, on evidence.** Applying
  it to multi-row answers made the offline regrade flip **11 correct recorded answers to wrong**:
  j01 ×3 and m02 ×3 for Haiku, and j01 ×2 and m02 ×3 for Sonnet. Haiku main would have dropped
  from 98.1% to 94.4%, and Sonnet main from 96.9% to 93.8%. These are legitimate answers with
  context columns between the label and the measure:
  - j01: `product_id, product_name, line_of_business, policy_count` against gold
    `(product_name, count)`;
  - m02: `line, GWP, incurred, loss_ratio` against gold `(lob, ratio)`.

  The gold SQL has no aliases that the by-name path could use, so those false negatives cannot
  be avoided that way. Multi-row answers therefore keep the bounded subset search and add the
  hedge rule. That rule fails the adversary's `9, 10, 11 / 19, 20, 21` case and passes the real
  context columns, which differ from the measure by orders of magnitude. `tests/test_round2.py`
  pins both j01 and m02 shapes as regressions.
- **The hedge rule is a heuristic.** Candidates more than 10% away from the gold value are not
  detected. This is documented in the README Limitations (R6).
- **A8 cannot be fully closed in code.** Evidence that has been forged and deleted leaves nothing
  to compare against. Git history is the control, as stated in the README.
- **Grader version bumped** from `v2-round1` to `v2-round2`, so the regraded files name the
  grader that actually produced them.
