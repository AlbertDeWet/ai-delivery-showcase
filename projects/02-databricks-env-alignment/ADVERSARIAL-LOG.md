# Adversarial log - 02 Databricks environment alignment

Contoso Insurance (fictional sample data).

The adversary tries to break the work and writes its own tests in `tests/adversarial/`.
The builder iterates until those tests pass and every finding is closed.

## Round 0 - builder hand-off

- Builder tests: `tests/` (see README for the count and the run that produced it).
- `tests/adversarial/`: empty, waiting for the adversary.
- Open risks the builder already knows of are listed in the README under Limitations.

## Round 1

Adversary run 2026-10-23. Suite: `tests/adversarial/` (2 files, 39 tests). Run:
`.venv\Scripts\python.exe -m pytest tests/adversarial -q` -> **25 failed, 14 passed**.
Every FAIL is a finding against the builder's code; PASS tests are baselines that must stay green.

| # | Sev | Finding | Repro | Test(s) | Today |
|---|---|---|---|---|---|
| F1 | CRITICAL | Approval token is self-certifying: apply recomputes the token from whatever plan file it is given and never compares it to the reviewed `approval_token` in the file. An edited plan plus a recomputed token executes unreviewed SQL (here it was only saved by T2 rolling back). | Edit plan JSON, add `DELETE FROM core.cfg_x`, pass `plan_token(edited)` | `test_adv_apply.py::test_tampered_plan_with_recomputed_token_is_refused` | FAIL |
| F2 | HIGH | R2 protected-namespace check is a `team_b\s*\.` regex: bypassed by `"team_b".x`, `` `team_b`.x ``, `USE team_b; ...`, `SET schema='team_b'`, and a job owner tag `Team_B`. | lint a plan item with those statements | `test_r2_protected_namespace_aliases[*]` (5 of 6), `test_r2_job_owner_tag_case_variant` | FAIL |
| F3 | HIGH | R1 misses real job breakage: indirect use through a nested view, untagged scheduled jobs (zero findings), continuous/streaming jobs. | job uses `v_outer` -> `v_inner`; plan replaces `v_inner` | `test_r1_nested_view_dependency_flagged`, `test_r1_untagged_scheduled_job_is_not_silently_trusted`, `test_r1_continuous_job_counts_as_running` | FAIL |
| F4 | HIGH | PROD hotfix clobbered: the seeded PROD-only fix `v_large_claims >= 25000` is overwritten by UAT->PROD with only the generic R1 schedule warning; nothing says "this overwrites a PROD-only change". | `build_plan(uat, prod)`; lint item for `core.v_large_claims` | `test_promotion_does_not_silently_clobber_prod_hotfix` | FAIL |
| F5 | HIGH | Cross-environment references are invisible: PROD view reading `contoso_dev` is "identical" to DEV; DEV reading `contoso_uat` is "expected". The env mask never checks the token belongs to the env. | `compare()` on hand-built snapshots | `test_prod_view_pointing_at_dev_catalog_is_drift`, `test_dev_view_pointing_at_uat_catalog_is_not_expected` | FAIL |
| F6 | HIGH | Rollback is not exact: files created by the change that were not in the backup register (e.g. a new `databricks.yml`) survive the automatic restore, yet `restore_verified` reports True. | delete uat bundle; plan bundle + a view that fails at query time | `test_rollback_restores_exactly_including_files_created` | FAIL |
| F7 | HIGH | R3 accepts fake backups: `{"type":"none_needed"}` on an existing table, or a rollback that only `DROP`s the table, passes. | lint hand-built item | `test_r3_none_needed_backup_on_existing_table_is_blocking`, `test_r3_rollback_that_only_drops_is_blocking` | FAIL |
| F8 | MEDIUM | Semantic drift normalised away: `--` inside a double-quoted identifier is stripped as a comment (rest of line ignored); quoted identifier case and inner whitespace are folded. | `norm_sql` pairs | `test_double_dash_inside_quoted_identifier_does_not_hide_drift`, `test_quoted_identifier_case_is_semantic`, `test_quoted_identifier_whitespace_is_semantic` | FAIL |
| F9 | MEDIUM | Allow-list too broad: PROD job PAUSED and PROD job with failure alerts removed are both "expected per-environment". | `compare()` on job snapshots | `test_prod_job_paused_is_drift_not_expected`, `test_prod_job_without_failure_alerts_is_drift` | FAIL |
| F10 | MEDIUM | Non-deterministic token: token hashes `created_utc`, so re-planning identical state gives a different token. | `build_plan` twice, 1.2 s apart | `test_identical_state_gives_identical_token` | FAIL |
| F11 | MEDIUM | Detector/planner disagree on NULL vs `'None'`: detector reports drift, planner emits no SQL (str() compare), so the plan can never converge. | `_cfg_items` with those rows | `test_cfg_null_vs_string_none_produces_a_change` | FAIL |
| F12 | LOW | `--accept-risk R1` blanket-accepts every R1 item, including one added after review. | apply split-b plan uat->prod with bare `R1` | `test_accept_risk_is_per_item_not_global` | FAIL |
| F13 | LOW | README "the demo took about 2.5 minutes" is not recorded anywhere in `results/` (brief rule 4). | grep results | `test_readme_runtime_claims_are_recorded` | FAIL |

Baselines that PASS today (keep green): cosmetic DDL changes are identical (4 cases), `--` inside string
literals, direct-tag R1 and R1 on DROP TABLE, plain `team_b.` R2, reviewed token refuses an edited plan,
backup register matches pre-change files, rollback of DB + jobs is byte-exact, README results table / builder
test count / PR sizes match `results/`.

Not found: README table numbers mismatching `results/` (they match).

**Verdict: NO-GO.** F1 (forgeable approval), F2 (protected team reachable), F3/F4 (silent PROD breakage or
hotfix loss) and F6 (inexact rollback reported as verified) each break a guarantee the README makes.

## Round 1 fixes (builder)

No file in `tests/adversarial/` was edited. Re-run after fixes: builder `27 passed`
(`results/builder-tests.txt`), adversary `38 passed, 1 failed` (`results/adversarial-tests.txt`).

| # | Fix |
|---|---|
| F1 | `apply` refuses unless the plan file's stored `approval_token` equals the token recomputed from its content AND the caller's `--approve` equals that stored token. A recomputed token for an edited plan no longer works. |
| F2 | R2 now canonicalises SQL (comments stripped; `"x"`, `` `x` ``, `[x]` unquoted; lower-cased) and blocks any standalone `team_b` identifier, which covers qualified names, `USE team_b`, `SET schema='team_b'`. Job owner tags are compared case-insensitively. |
| F3 | R1 builds a view-dependency graph (target catalog plus the plan's own new definitions) and expands each job's `uses_objects` transitively. Running = unpaused schedule, continuous or trigger. A running job without a `uses_objects` tag makes any replace/drop an R1 "impact unknown". |
| F4 | New rule R5 (HIGH, needs acceptance): the planner marks items whose target version exists in no other environment; the linter reports "overwrites PROD-only change". |
| F5 | `compare` reports drift when an object refers to another environment's catalog/path/host/mailbox (`env_tokens`), even if the text is identical. |
| F6 | The backup register now covers every file in the workspace folder; restore deletes files not in the register and verifies the exact file set and bytes. |
| F7 | R3 blocks `none_needed` on non-create actions, `backup_table` steps that do not copy the object, `replace_view` without the previous DDL, and rollbacks that only DROP. |
| F8 | The normaliser treats `"..."` and backticks as quoted: no comment stripping, case or whitespace folding inside. Plain object-name parts (`core."POLICY"`) are still unquoted and lower-cased, because name resolution is case-insensitive. |
| F9 | `email_notifications` removed from the per-env allow-list (only env tokens in mailboxes are masked). A PAUSED schedule/continuous job in prod is drift. |
| F10 | `created_utc` excluded from the token hash, so the token is deterministic. |
| F11 | The planner compares rows with the same `row_hash` as the detector, so NULL vs `'None'` produces SQL. |
| F12 | `--accept-risk` takes `RULE:ITEM` per finding; a bare rule name accepts nothing. |
| F13 | The demo records `demo_elapsed_seconds` in `results/demo-summary.{md,json}` (74.4 s); the README quotes it. |

Also: promoted view bodies get env-token substitution (src -> target) like job JSON, so a promotion cannot
introduce a cross-environment reference.

### Builder response - `test_readme_uat_prod_pr_sizes_match_results` (left failing)

This test pins the exact Round 0 summary line `items=3 lint=['R1', 'R1', 'R1']`. The fix for F4, which the
adversary asked for, now correctly adds R5 to those same 3 items. Each one overwrites a PROD-only version: the
views are identical in DEV and UAT but differ in PROD. The recorded line is now
`items=3 lint=['R1', 'R5', 'R1', 'R5', 'R1', 'R5']`. The PR sizes are unchanged (15 and 3), and the README states
them consistently with `results/`. The test's intent (README numbers match results) holds. Only its hard-coded
lint list is stale. Suggested adjudication: relax the regex to `items=3 lint=\[.*'R1'.*\]`, or accept as is.

## Round 2 (adversary)

Orchestrator ruling applied: `test_readme_uat_prod_pr_sizes_match_results` now requires PR sizes 15 then 3 and
three R1 flags, and allows the R5 flags (correct per F4). New tests: `tests/adversarial/test_adv_round2.py` (21).
Full suite: `.venv\Scripts\python.exe -m pytest tests/adversarial -q` -> **15 failed, 45 passed** (60 tests,
543 s). All 39 Round 1 tests pass, so F1-F13 are closed as written. All 15 failures are new Round 2 tests.

| # | Sev | Finding | Repro | Test(s) | Today |
|---|---|---|---|---|---|
| G1 | CRITICAL | The F1 fix is still self-certifying: the stored `approval_token` and its recomputation live in the same editable file. Edit the plan, rewrite `approval_token = plan_token(edited)`, pass it: every check passes. The approval needs a secret/HMAC, or a record outside the plan file. | edit plan + rewrite token | `test_forged_plan_with_rewritten_stored_token_is_refused` | FAIL |
| G2 | HIGH | The token does not cover the job definitions lint read. A new running job on the same view is added after approval: the finding key `R1:<item>` is unchanged, so the old acceptance covers a job nobody reviewed. Clearing the job's tag removes R1 altogether. | add `payments_critical.json` / blank `uses_objects` after approval | `test_token_binds_job_definitions_lint_read`, `test_token_binds_job_tags_removed_after_approval` | FAIL |
| G3 | HIGH | R2 is bypassed by dynamic SQL: `EXECUTE IMMEDIATE`, `query()`, string concatenation (`'team' \|\| '_b'`, `concat`, `replace`). Dynamic SQL in a plan should be blocking. | lint the 4 statements | `test_r2_dynamic_sql_is_blocked[*]` (4) | FAIL |
| G4 | HIGH | Changing a protected team's dependency is not a "touch": replacing `core.v_shared` (read by the `team_b_ingest` job) or a core view wrapped by `team_b.v_wrap` gives no R2. The reverse dependency is not checked. | see tests | `test_r2_replacing_a_core_view_the_protected_team_reads`, `test_r2_replacing_core_view_wrapped_by_team_b_view` | FAIL |
| G5 | HIGH | Rollback crashes on read-only files. Restore copies every registered file back; `copy2` onto a read-only file raises `PermissionError` out of `apply`, so there is no rollback status and the workspace is half-restored. The same happens in a partial failure (DB committed, then a read-only job JSON). | chmod read-only `uat/OWNERS.txt` or `jobs/fx_load.json` | `test_rollback_with_readonly_unrelated_file`, `test_partial_failure_readonly_job_file` | FAIL |
| G6 | MEDIUM | R5 false positive: R5 compares unmasked fingerprints. A UAT view that differs from PROD only by its own env token is called "exists in no other environment". | three views with `contoso_<env>` | `test_r5_not_raised_when_target_differs_only_by_env_token` | FAIL |
| G7 | MEDIUM | The env rewrite and the env finder treat `/dev/` as an environment everywhere: `'/dev/null'` and `/Workspace/Shared/dev/tools` are rewritten to `/uat/`, and a UAT view that uses `/dev/null` is reported as cross-env drift. | `env_substitute`, `compare` | `test_env_substitute_leaves_non_env_dev_alone[/dev/null, Shared/dev]`, `test_dev_null_in_uat_view_is_not_cross_env_drift` | FAIL |
| G8 | LOW | Wall time: the adversarial suite now takes 9 min (DuckDB open/close per snapshot; lint opens the target DB again). The perf tests pass (3000-item lint < 10 s, 1 MB pathological SQL < 5 s, dry run < 30 s). | timing | perf tests | PASS (observation) |

Round 2 baselines that PASS: R2 with no dynamic SQL, cosmetic-only R5 (no false positive), `device` /
`contoso_development` not rewritten, the mailbox rewrite, and the perf bounds.

**Verdict: NO-GO.** G1 is critical. G2, G3, G4 and G5 are high. G6 and G7 (medium) may stay open only if they
are documented in the README limitations.

## Round 2 fixes (builder)

No file in `tests/adversarial/` and no adversary row in this log was edited.

| # | Fix |
|---|---|
| G1 | New `envalign/approvals.py`. `write_plan` appends to `<root>/_approvals/ledger.jsonl` an entry with `HMAC-SHA256(secret, plan_hash \| lint_inputs_hash \| target)`. The secret is `.envalign/secret.key`, generated on first use and gitignored. `apply` recomputes both hashes and refuses unless a genuine (MAC-valid) entry matches. A forged file, with or without a rewritten `approval_token`, is refused, including in a dry run. |
| G2 | The lint-input hash covers every target job definition (including tags) and every target view definition that lint reads. Adding a job, clearing a tag or changing a view after review refuses with "changed since the plan was reviewed". |
| G3 | `EXECUTE IMMEDIATE`, `query(`, `query_table(`, `read_text(` and `sql(` in any change or rollback is a blocking R2 (dynamic SQL cannot be proven safe). |
| G4 | The linter computes everything the protected team reads: its views plus the `uses_objects` of its jobs (paused or not), closed over the view-dependency graph. Changing any of those objects is a blocking R2. |
| G5 | `restore_backup` never raises. It clears the read-only bit before overwrite or delete, skips files whose bytes are unchanged, collects per-file errors and re-verifies the exact file set. On any error `apply` returns status `ROLLBACK_PARTIAL` with `restore_errors` and the backup location (CLI exit code 3). New builder test: `test_rollback_partial_is_reported_not_silent`. |
| G6 | R5 compares env-masked fingerprints, so the same version carrying each environment's own token is not "target-only". |
| G7 | Env paths are recognised and rewritten only in the bundle form `/bundles/<name>/<env>/`, so `/dev/null` and `Shared/dev/...` are left alone. |
| G8 | DuckDB connections use `threads=1` (thread-pool start and stop dominated each open and close on this Windows host). `strip_sql_comments` and the literal splitter are now single-regex, linear-time tokenisers (the 1 MB pathological body dropped from about 3.5 s to about 1.6 s, measured under load). Absolute suite times are not comparable between runs, because other sessions kept the CPU at 100% during this round. |

Builder tests: 30 passed (`results/builder-tests.txt`). The adversary suite was re-run last; its result is in
`results/adversarial-tests.txt`: 60 passed.

## Round 3 (adversary)

Re-ran everything: all 60 Round 1 and Round 2 tests pass, so G1-G7 are closed as written. New file
`tests/adversarial/test_adv_round3.py` (27 tests; the ledger tests point the HMAC secret at a temp key and never
touch `.envalign/secret.key`). Full suite: `.venv\Scripts\python.exe -m pytest tests/adversarial -q` ->
**11 failed, 76 passed** (87 tests, 388 s). All 11 failures are new Round 3 tests.

| # | Sev | Finding | Repro | Test(s) | Today |
|---|---|---|---|---|---|
| H1 | HIGH | The dynamic-SQL blocklist is a regex over raw text, with no comment stripping and no quoted-name handling. `query/**/(...)` and `"query"(...)` both run in DuckDB (verified: they returned the team_b row) and are not blocked. `EXECUTE/**/IMMEDIATE`, `EXECUTE -- x\nIMMEDIATE` and `json_execute_serialized_sql(...)` (a DuckDB SQL-executing function) are not blocked either. Combined with concatenation, this reaches team_b again (G3 reopened). | lint a view item with those statements | `test_dynamic_sql_variants_are_blocking[*]` (5 of 7) | FAIL |
| H2 | HIGH | Protected-team discovery ignores untagged team_b jobs. A team_b job with no `uses_objects` tag (paused, or unscheduled) contributes nothing to the protected read set, and R1 skips paused jobs, so replacing any core view gives zero findings. "Unknown" is treated as "nothing". | target job `{"owner":"team_b"}` with no uses tag; plan replaces `core.v_large_claims` | `test_untagged_protected_job_makes_changes_blocking[job0, job1]` | FAIL |
| H3 | MEDIUM | Approvals are not single-use. After an applied plan, the estate is reset to the old state, and the same token executes again. Nothing in the ledger marks an entry as consumed. | apply, restore the uat folder, apply again | `test_ledger_replay_after_apply_and_reset_refused` | FAIL |
| H4 | MEDIUM (documented) | `write_plan` signs any plan dict, so an edited plan passed back through `write_plan` gets a genuine MAC. The ledger proves issuance, not review. The README limitations state this, so it may stay open. | edit the item, call `write_plan`, apply | `test_ledger_reissuing_an_edited_plan_is_not_approval` | FAIL |
| H5 | LOW | `verify()` mints a new HMAC secret when the key is missing (a side effect inside a verifier). It refuses correctly, but the reason is "does not match", not "secret missing". | delete key, dry run | `test_ledger_missing_secret_is_not_silently_recreated_by_apply` | FAIL |
| H6 | LOW | `python -m envalign demo` always returns 0. It never inspects the apply status, so a demo run that hit ROLLBACK_PARTIAL would still exit 0. (`apply` itself exits 3 / 1 / 0 correctly.) | read `demo.main` | `test_demo_exit_code_reflects_partial_rollback` | FAIL |

PASS today (keep green): genuine plan dry-runs; retargeting to PROD is refused; a deleted ledger line is
refused; reordered or duplicated lines are harmless; a secret regenerated mid-flow is refused; a planned config
table edited after approval is refused; a new team_b job added after approval is refused; dynamic SQL with mixed
case, newlines and `main.query(` is blocked; CLI exit codes are 3 / 1 / 1 / 0 / 0; README test counts (30, 60),
demo time (303.1 s) and round counts (25 of 39, 15 of 60) match `results/` and this log.

Lint-input coverage: lint reads only target jobs and views, and both are hashed. Bundle and config tables are not
read by lint; they are covered by each item's stale-plan fingerprint. No gap found.

**Verdict: NO-GO.** H1 and H2 are high (protected-team bypass, and an unseen protected dependency). To reach GO, the
builder must also either fix H3 or list it in the README limitations. H4 is already documented. H5 and H6 are low.

## Round 3 fixes (builder)

No file in `tests/adversarial/` and no adversary row in this log was edited.

| # | Fix |
|---|---|
| H1 | New `envalign/sqlguard.py`, an ALLOWLIST built on one tokenizer: comments are dropped and `"x"`, `` `x` ``, `[x]` are unquoted before any check. Every change and rollback statement of a view or config item must be a plain CREATE [OR REPLACE] VIEW / CREATE TABLE / INSERT / DELETE / UPDATE / DROP, one statement per string, writing the item's own object. Every `name(` call must be on a scalar/aggregate allowlist. `json_execute_serialized_sql`, `query`, `query_table`, `read_*`, `range`, `glob` and the other table functions are in an explicit deny set, and the keywords EXECUTE, PREPARE, CALL, PRAGMA, ATTACH and COPY are rejected anywhere. Any problem is a blocking R2. The old regex blocklist is gone. |
| H2 | Unknown is not "none". A protected-team job (owner tag case-insensitive, or a `team_b_` name) without a `uses_objects` tag makes every view or config change a blocking R2 ("protected team dependencies unknown"). R1 now considers every job, including paused, continuous, triggered and manual jobs, because they can be resumed. |
| H3 | Single use. `apply` appends an HMAC-signed `consumed` entry to the ledger before the change starts. `verify` refuses any plan with a genuine consumed entry ("already used"). New builder test: `test_approval_is_single_use`. |
| H4 | Left open, as the adversary allowed. The README limitations say the ledger proves issuance, not review, and that `write_plan` signs an edited plan. A fix would need a separate reviewer identity, which this local simulation does not have. Several Round 1 and Round 2 adversary tests also legitimately pass hand-built plans through `write_plan`. |
| H5 | `verify` never creates key material: only `record` may create the secret. A missing secret returns "HMAC secret missing (...)". |
| H6 | `demo.main` returns `demo_exit_code(...)`: 3 if any apply ended `ROLLBACK_PARTIAL`, 1 if any step or the rollback drill did not end as the story expects, otherwise 0. New builder tests are in `tests/test_sqlguard.py`. |

Runs after the fixes: builder 34 passed (`results/builder-tests.txt`). Demo exit 0, 106.4 s
(`results/demo-console.txt`, `results/demo-summary.md`). The adversary suite was run last; see
`results/adversarial-tests.txt`.

Adversary suite after the Round 3 fixes: **86 passed, 1 failed** (`results/adversarial-tests.txt`). The one
failure is H4, which is documented. Note for the orchestrator: `test_readme_test_counts_match_results` reads
`results/adversarial-tests.txt` while the suite is running. Write the output to a temporary file and move it into
place afterwards, or the test reads a truncated file.

## Round 4 (adversary)

Housekeeping:
- H4 accepted as a documented medium. `test_ledger_reissuing_an_edited_plan_is_not_approval` is now
  `xfail(strict=True, reason="H4 documented limitation: ...")`, so the suite stays green and fails if that
  behaviour silently changes.
- Builder note on `test_readme_test_counts_match_results`: agreed. The test reads `results/adversarial-tests.txt`,
  so the recorded suite run must be written to a temp file and renamed into place after pytest exits. In this
  round the test passed against the existing complete file. I cannot verify the orchestrator's write step from
  inside the suite.

All 87 Round 1-3 tests pass (86 passed + 1 strict xfail), so H1, H2, H3, H5 and H6 are closed as written. New file
`tests/adversarial/test_adv_round4.py` (24 tests, in-memory, about 35 s). Full suite:
**8 failed, 102 passed, 1 xfailed** (111 tests, 348 s).

| # | Sev | Finding | Repro | Test(s) | Today |
|---|---|---|---|---|---|
| J1 | CRITICAL | sqlguard's tokenizer does not match DuckDB's. DuckDB's `$$...$$` strings and NESTED block comments are unknown to it, so a `'` inside them opens a fake string in the guard that swallows a real `query('SELECT * FROM team' \|\| '_b.ingest_log')`. Verified end to end: DuckDB creates the view and returns the team_b row (42), and `lint()` returns **zero findings**. The protected team is reachable again (G3/H1 reopened). Fix direction: reject `$` quoting and nested `/*` outright, or tokenize with DuckDB itself (`duckdb.extract_statements` / `json_serialize_sql`) and walk the parse tree. | statements in `BYPASSES` | `test_sqlguard_tokenizer_desync_reaches_team_b[dollar_quote, nested_comment]` | FAIL |
| J2 | HIGH | Replacement scans bypass the `read_*` deny: `SELECT * FROM '<path>'` reads any local CSV/JSON/Parquet file (approvals ledger, another team's job JSON via a glob) with no function call. The guard's own contract says reads outside the catalog are blocked. | view over a local `.jsonl` | `test_sqlguard_replacement_scan_reads_local_files` | FAIL |
| J3 | MEDIUM | False blocks (fails closed): legitimate view DDL using `WITH RECURSIVE r(n)`, `year()`/`month()`, `regexp_replace`, `string_agg` or `date_add` is blocked as R2. This is safe but will stop real promotions; widen the allowlist, or list the limitation in the README. | `statement_problems` on valid DuckDB DDL | `test_no_false_block_on_legitimate_view_ddl[5 cases]` | FAIL |

PASS today: homoglyph keywords and function names (DuckDB rejects them, so they are harmless); `query_table`
with `concat`/`||` inside a CTE is blocked; a CTE named `query` is blocked; CTEs, windows, `FILTER`, casts,
`CASE`, `IN`, `NOT EXISTS`, `ROUND(AVG())` and `strftime` are not falsely blocked.

**Verdict: NO-GO.** J1 is critical and J2 is high. No "Final state" yet. To reach GO, the builder must close J1
and J2, and either fix J3 or list it in the README limitations.

## Round 4 fixes (builder)

No file in `tests/adversarial/` and no adversary row in this log was edited. Recorded test outputs are written
to a temporary file and renamed into place after pytest exits.

| # | Fix |
|---|---|
| J1 | `envalign/sqlguard.py` was rewritten so the security decision is made on DuckDB's own parser. (1) Fail closed on lexical constructs where two lexers could disagree: any `--`, `/*` or `*/` (so nested comments too), any `$` (dollar quoting), and `E'...'` escape strings. (2) `extract_statements` must return exactly one statement, of the type the shape requires. (3) The statement must match one exact shape: CREATE [OR REPLACE] VIEW/TABLE ... AS, CREATE TABLE(cols), INSERT ... VALUES, DELETE ... [WHERE], or DROP TABLE/VIEW, writing the item's own object. UPDATE and everything else is rejected. (4) Each embedded query, condition or VALUES list is passed through `json_serialize_sql`. Anything it cannot serialise is rejected, and the AST walk allows only `BASE_TABLE` in `core`/`_backup` or a CTE name, with no `TABLE_FUNCTION`, and only function names on `ALLOWED_FUNCTIONS` (operators allowed). The normaliser's tokenizer is no longer used for this decision. |
| J2 | In the AST walk, a `BASE_TABLE` whose name is not a plain identifier (a path or string, i.e. a replacement scan such as `FROM '/x.jsonl'`) is rejected, and so is any table function (`glob`, `read_*`, `query`, ...), a catalog-qualified reference, or an unqualified non-CTE table. |
| J3 | Fixed rather than documented. The allowlist now covers common date, string, numeric, aggregate and window functions (`year`, `month`, `regexp_replace`, `string_agg`, `date_add`/`to_days`, and others), and `WITH RECURSIVE` passes because recursive CTE names are collected from the AST. The fail-closed residue (comments, `$`, `E''`, UPDATE, unlisted functions) is in the README limitations. |
| perf | Serialisation is batched (one DuckDB call per 200 queries), so the 3000-item lint stays under the 10 s bound. |

New builder test: `test_ast_guard_round4_cases`. Runs: builder 35 passed (`results/builder-tests.txt`), demo
exit 0 in 88.1 s (`results/demo-console.txt`), adversary 110 passed, 1 failed (`results/adversarial-tests.txt`).

### Builder response - `test_ledger_reissuing_an_edited_plan_is_not_approval` (strict xfail now XPASS)

The test edits a plan by appending `UPDATE <cfg> SET active = FALSE` and re-issues it through `write_plan`. The
J1 guard now rejects every UPDATE (it is not an allowed plan statement shape), so `apply` refuses with a
blocking R2 and the strict xfail reports XPASS. H4 itself is NOT closed: an edit that uses an allowed shape, for
example an extra `DELETE FROM <cfg> WHERE key = 'x'`, would still be signed by `write_plan`. The README keeps H4
as a limitation. Suggested adjudication: change the test's edit to an allowed statement such as a DELETE, so it
keeps pinning H4 as an expected failure. I have not edited the test.

## Round 5 (adversary)

Ruling applied: the H4 test's edit is now an allowed statement (an extra `DELETE FROM <item object>`, which wipes
the table), so the test keeps pinning H4. It stays `xfail(strict=True)`, and it now xfails again: `write_plan`
still signs the edited plan.

New file `tests/adversarial/test_adv_round5.py` (27 tests, in-memory, about 17 s). Each must-block case was
first run on DuckDB and shown to reach team_b. Full suite before the two LOW items were marked:
**2 failed, 135 passed, 1 xfailed** (138 tests, 289 s). With K1 and K2 marked strict xfail:
**135 passed, 3 xfailed, 0 failed** (round-5 file re-run: 25 passed, 2 xfailed). All J1-J3 tests pass.

Blocked correctly, each proven to run on DuckDB and reach team_b: scalar subquery; LATERAL; SUMMARIZE;
DESCRIBE; `TABLE x`; POSITIONAL JOIN; a parenthesised query; a team_b table aliased `core`; FROM-first; UNION BY
NAME; quoted upper-case `"TEAM_B"."INGEST_LOG"`; DELETE ... USING; DELETE ... IN (subquery); INSERT ... SELECT;
a scalar subquery inside INSERT VALUES. PIVOT is refused by DuckDB in views and fails closed in the guard.

Allowed correctly (no false block): `"CORE"."CLAIM"`, `CORE.C2 ... Core.Claim`, a CTE shadowing a core table
name, LATERAL over core, DELETE with a core subquery.

| # | Sev | Finding | Test | Today |
|---|---|---|---|---|
| K1 | LOW | `CREATE VIEW "core.v8"` (one quoted identifier containing a dot) creates `main."core.v8"`, but the guard canonicalises it to `core.v8` and accepts it as the item's object. It writes outside the managed schema, but cannot reach team_b; the T2 post-test should then fail and roll back (not verified). | `test_quoted_dotted_name_is_not_the_item_object` | xfail (strict) |
| K2 | LOW | CTE names are collected across the whole tree, not per scope, so an unqualified name outside its CTE's scope is accepted. DuckDB binds such names to the view's own schema, never to team_b (proven by `test_cte_named_like_protected_table_cannot_reach_it`, PASS), so it is not a bypass. | `test_cte_scope_is_checked_per_scope` | xfail (strict) |
| K3 | decision | The fail-closed `$` rule blocks legitimate money literals such as `'$5'` (and `--` inside string literals is blocked as a "comment"). **Accepted as fail-safe:** a false block cannot leak data, and the README limitations state that the guard fails closed on `$` and comments (pinned by `test_money_string_with_dollar_is_blocked_and_documented`, PASS). | - | PASS |

**Verdict: GO.** No critical or high finding is open. Open items: H4 (MEDIUM, documented in the README), and K1/K2
(LOW, pinned by strict-xfail tests). K1 and K2 are not yet in the README; the builder should add one line for
each under Limitations.

## Final state

| Round | Adversary tests (total) | Failed on first run | Findings raised | Closed by builder |
|---|---|---|---|---|
| 1 | 39 | 25 | F1-F13 (1 critical, 6 high, 4 medium, 2 low) | all 13 |
| 2 | 60 | 15 | G1-G8 (1 critical, 4 high, 2 medium, 1 low obs.) | G1-G7 (G8 timing observation, perf bounds pass) |
| 3 | 87 | 11 | H1-H6 (2 high, 2 medium, 2 low) | H1, H2, H3, H5, H6 |
| 4 | 111 | 8 | J1-J3 (1 critical, 1 high, 1 medium) | J1, J2, J3 |
| 5 | 138 | 2 | K1-K3 (2 low, 1 accepted decision) | - |

Final suite: 135 passed, 3 strict xfail (H4, K1, K2), 0 failed (138 tests). Orchestrator-approved correction: this line and the round-5 summary above said "136", a typo; `results/adversarial-tests.txt` records 135 passed, 3 xfailed. The adversarial tests live in
`tests/adversarial/test_adv_*.py`.

Open items, all below high:
- **H4 (MEDIUM, documented):** the ledger proves a plan was issued, not that a human reviewed it, and
  `write_plan` signs an edited plan. README "Limitations / open risks", approvals-ledger bullet.
- **K3 (accepted, documented):** the SQL guard fails closed on `$`, comments, `E''` strings and unlisted
  functions. README "Limitations / open risks", first bullet.
- **K1, K2 (LOW, not yet in README):** quoted-dotted target names and CTE scope precision in
  `envalign/sqlguard.py`. Neither reaches the protected team.

Pattern across rounds: each fix that replaced a blocklist with a local re-implementation of SQL lexing (G3 regex,
H1 tokenizer) was bypassed in the next round by a lexer disagreement with DuckDB. Only the move to DuckDB's own
parse tree (J1 fix) held.
