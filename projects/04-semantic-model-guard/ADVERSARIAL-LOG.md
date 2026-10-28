# Adversarial log - 04 Semantic model guard

| Round | Adversary tests added | Failed first | Findings | Fixes | Final state |
|---|---|---|---|---|---|
| 1 | 36 | 25 | 21 (6 critical, 7 high, 6 medium, 2 low) | all 21 (see Round 1 fixes) | fixed; awaiting Round 2 |
| 2 | 35 | 21 | 21 (8 critical, 7 high, 5 medium, 1 low) | all 21 (see Round 2 fixes) | fixed; reopened by Round 3 |
| 3 | 31 | 15 | 14 (3 critical, 7 high, 3 medium, 1 low) | all 14 (see Round 3 fixes) | fixed; reopened by Round 4 |
| 4 | 31 | 17 | 7 (0 critical, 5 high, 2 medium) | all (see Round 4 fixes) | fixed; reopened by Round 5 |
| 5 | 39 | 20 | 13 (0 critical, 3 high, 5 medium, 5 low) | all 13 (see Round 5 fixes); 3 disputed tests adjudicated in Round 6 | fixed; reopened by Round 6 |
| 6 | 38 | 12 | 5 (0 critical, 2 high, 3 medium) | all 5 (see Round 6 fixes) | fixed; reopened by Round 7 |
| 7 | 14 | 2 | 2 (0 critical, 2 high) | both (see Round 7 fixes) | fixed; adversary verified 14/14 - **GO** |

## Builder's known open risks (handed to the adversary as starting points)
See README "Limitations".

## Round 1

Adversary tests: `tests/adversarial/test_adv_trim_safety.py`, `test_adv_detect_regress.py`,
`test_adv_backup_drift_readme.py` (helpers in `helpers.py`). Run:
`.venv\Scripts\python -m pytest tests/adversarial`. Result today: **36 tests, 25 FAIL, 11 PASS.**

Severity: CRITICAL = trim deletes something in use or destroys data; HIGH = a real regression or break is
hidden or invented; MEDIUM = wrong report/noise; LOW = labelling.

### Findings

| # | Sev | Finding | Repro (short) | Test | Today |
|---|---|---|---|---|---|
| 1 | CRITICAL | `apply()` runs `rmtree(work_dir)` first; a work_dir that is or contains the model folder **deletes the original model** - the "original is never touched" guarantee breaks | `apply(model, ..., work_dir=model.parent, plan)` | test_apply_with_workdir_equal_to_model_does_not_destroy_original | FAIL |
| 2 | CRITICAL | `rollback()` deletes the target before checking the backup exists; with no backup the model is lost | `rollback(missing_work, model)` | test_rollback_without_backup_does_not_delete_target | FAIL |
| 3 | CRITICAL | Multi-line measures (`measure X =` + indented body, or ``` fenced) parse to an empty / "```" expression, so their columns are trimmed and the measure silently breaks | measure body on next line using ReserveAmount -> ReserveAmount planned for removal | test_multiline_measure_columns_not_trimmed, test_multiline_measure_expression_parsed | FAIL |
| 4 | CRITICAL | Visual-level and report-level filters are not read; a column used only as a filter is trimmed (and the consumer check still says 3/3 pass) | `filterConfig` on ClaimStatus / LineOfBusiness | test_visual_filter_column_not_trimmed, test_report_level_filter_column_not_trimmed | FAIL |
| 5 | CRITICAL | Calculated columns (`column X = expr`) are not parsed at all; their source columns are trimmed | 'Net Amount' = PaidAmount - ReserveAmount on a visual -> ReserveAmount removed | test_calculated_column_is_parsed, test_calculated_column_source_not_trimmed | FAIL |
| 6 | CRITICAL | RLS-only, hierarchy-level-only and sortByColumn-target columns are trimmed (builder-listed, confirmed) | roles/*.tmdl filter on Region; hierarchy level Month; MonthName sortByColumn Month | test_rls_only_column_not_trimmed, test_hierarchy_level_column_not_trimmed, test_sort_by_column_target_not_trimmed | FAIL |
| 7 | HIGH | Regression grader hides a real model change as "EXPECTED: fresh data" when the change only touches values absent from frozen data | filter "Reopened" changed to "Closed"; frozen data has neither | test_model_change_masked_as_fresh_data | FAIL |
| 8 | HIGH | A VALID but untranslatable measure (SUMX, VAR...) has error checks before, so if it disappears it is graded "EXPECTED: broken before; removed by trim" - never regression-tested, label is false | `classify({"total::M": {"error": "untranslatable..."}}, {}, {})` | test_untranslatable_measure_removed_is_not_silently_expected | FAIL |
| 9 | HIGH | Implicit-measure (`Aggregation`) projections crash `report_fields` (KeyError 'SourceRef'); real PBIR reports use them constantly | sum-of-column in a visual | test_aggregation_projection_does_not_crash | FAIL |
| 10 | HIGH | Broken-measure false positive: `[Name]` inside a `//` comment flags a valid measure as broken -> it would be planned for deletion | `X = SUM(...) // was [Old Paid]` | test_comment_reference_is_not_a_break | FAIL |
| 11 | HIGH | False positive: DAX is case-insensitive; `factclaims[paidamount]` is reported missing -> valid measure deleted | | test_case_insensitive_table_name_is_not_a_break | FAIL |
| 12 | HIGH | False positive: table-qualified measure ref `FactClaims[Total Paid]` (legal DAX) reported as missing column -> deleted | | test_table_qualified_measure_ref_is_not_a_break | FAIL |
| 13 | HIGH | False negative: `COUNTROWS(GhostTable)` (missing table) is not detected as broken | | test_missing_table_in_countrows_is_detected | FAIL |
| 14 | MEDIUM | Trim rewrites every TMDL file: CRLF -> LF and adds a trailing newline even when nothing was removed (noisy diffs, not "everything else intact") | DimDate.tmdl with CRLF, nothing removed | test_trim_does_not_rewrite_untouched_files | FAIL |
| 15 | MEDIUM | Drift: column dataType differences are not reported | Year int64 vs string | test_drift_detects_datatype_change | FAIL |
| 16 | MEDIUM | Drift: relationships are not compared at all | rel2 missing in PROD | test_drift_detects_relationship_difference | FAIL |
| 17 | MEDIUM | Drift: whitespace-only expression difference reported as drift (false positive) | `SUM( x )` vs `SUM(x)` | test_drift_ignores_whitespace_only_differences | FAIL |
| 18 | MEDIUM | Consumer check ignores the visual's Entity for measures; a measure bound to a non-existent table passes | Entity "GhostTable" | test_consumer_check_unknown_table_for_measure | FAIL |
| 19 | LOW | A brand-new check is labelled "was broken before, resolves after" | `classify({}, new, new)` | test_new_check_not_labelled_broken_before | FAIL |
| 20 | LOW | README still says "No adversarial round has run yet" while Round 1 is logged | README Limitations + "How the AI method was applied" | test_readme_claims_no_adversarial_round_is_stale | FAIL (now that Round 1 is logged) |
| 21 | MEDIUM | Regression suite only joins DimDate/DimPolicy and groups by Year/LOB; DimProduct paths, relationship direction and other groupings are never exercised (no test; by inspection of `regress.JOINS`) | - | - | not tested |

### Attacks that held (PASS today)
- Measure-to-measure chains (3 deep) keep the leaf column.
- Quoted table/column names with spaces resolve.
- Self-reference and bare `[GhostCol]` are detected as broken.
- `None` before vs `0.0` after is flagged UNEXPECTED.
- Rollback after apply + damage is byte-exact; corrupting the backup is caught by `_same_tree`.
- Measure moved to another table is detected as drift.
- README results table and prose counts match `results/summary.json`; builder test count (17) matches;
  a fresh demo run reproduces `results/summary.json` exactly.

### Verdict Round 1: **NO-GO**
Six critical findings mean the trim can delete columns that are in use (multi-line measures, filters,
calculated columns, RLS, hierarchies, sort-by) and two code paths can destroy the original model.
The tool's own safety checks report `safe: true` in every one of these cases. Builder must fix
findings 1-13 at minimum and narrow the README claims before the next round.

## Round 1 fixes (builder)

No adversary test was edited and no test is disputed (no "Builder response" needed).

| # | Fix |
|---|---|
| 1 | `apply()` refuses a work_dir that is, contains or sits inside the model; it only deletes a work_dir carrying its own `.smguard-work` marker; backup is verified byte-identical before anything is applied. |
| 2 | `rollback()` checks the backup exists first, copies it to a staging folder, verifies it, and only then replaces the target. |
| 3 | New block-based TMDL parser: multi-line indented and ``` fenced expressions. |
| 4 | Report fields are found by walking every JSON field binding in visual.json, page.json and report.json (projections, filterConfig at all levels, Aggregation / implicit measures). |
| 5 | Calculated columns parsed; their source columns count as used. |
| 6 | RLS `tablePermission` filters (definition/roles), hierarchy levels and sortByColumn targets count as uses. Generator now has a sortByColumn (MonthName -> Month). |
| 7 | Every regression check carries its expanded definition; any definition change is UNEXPECTED, so "fresh data" requires an unchanged definition. |
| 8 | Untranslatable (valid) measures are UNTESTED, and UNEXPECTED if they disappear; only measures the analyzer proved broken can be "EXPECTED: broken before". Generator now includes one (`AVERAGEX`) so the demo shows UNTESTED. Their columns are always kept (they are valid measures). |
| 9 | Aggregation projections no longer crash (generic walker). |
| 10 | `//`, `--` and `/* */` comments are stripped before reference extraction (string literals preserved). |
| 11 | All name lookups are case-insensitive. |
| 12 | `T[x]` resolves to a column, else to a measure x (table-qualified measure ref). |
| 13 | Table-only refs (`COUNTROWS(T)`, `FILTER(T, ...)`, `SUMX(T, ...)` ...) are checked; missing table = broken. |
| 14 | `remove_objects` only rewrites files where something was removed, keeping original line endings; `apply` adds an `untouched_files_byte_identical` check. |
| 15-17 | Drift compares dataType / calculated expression / sortByColumn, relationships, measure home table, and normalises whitespace and comments. |
| 18 | Consumer check fails on a binding to a missing table, or a measure bound to the wrong table. |
| 19 | A check that did not exist before is `NEW`, not "broken before". |
| 20 | README updated for Round 1. |
| 21 | Regression joins are built from the model's relationships (DimDate, DimPolicy, DimProduct) and a `by_product` grouping was added. |

Re-run after fixes: builder 19/19 pass, adversary 36/36 pass. Demo: 0 UNEXPECTED in all three
environments; 4 UNTESTED checks per environment (`[Avg Net per Claim]`). Awaiting Round 2.

## Round 2

Re-run first (before adding Round 2 tests): **55/55 pass** (19 builder + 36 adversary) - builder claim confirmed.
Round 2 tests: `tests/adversarial/test_adv_round2.py`, 35 tests (one parametrised x3).
Whole suite now: **90 tests, 21 FAIL, 69 PASS** (all 21 failures are Round 2 tests; every Round 1 test still passes).

| # | Sev | Finding | Test | Today |
|---|---|---|---|---|
| R2-1 | CRITICAL | Column used only in a calculation item is trimmed; no "unsupported feature" refusal | test_calculation_group_column_kept_or_refused | FAIL |
| R2-2 | CRITICAL | Column listed in a perspective is trimmed -> perspective references a missing column, model invalid | test_perspective_column_kept_or_refused | FAIL |
| R2-3 | CRITICAL | Column in OLS `columnPermission` is trimmed -> role references a missing column, model invalid | test_ols_column_permission_kept_or_refused | FAIL |
| R2-4 | CRITICAL | Column named only in a field parameter (`NAMEOF` in a calculated partition) is trimmed | test_field_parameter_column_kept_or_refused | FAIL |
| R2-5 | CRITICAL | Column used only by a calculated table's DAX source is trimmed (NEW, not in limitations) | test_calculated_table_column_kept_or_refused | FAIL |
| R2-6 | CRITICAL | Column used only in a measure's `formatStringDefinition` (dynamic format string) is trimmed (NEW) | test_dynamic_format_string_column_kept | FAIL |
| R2-7 | CRITICAL | `--` inside a column name is treated as a comment; the reference vanishes and the in-use column is trimmed (NEW, introduced by the Round 1 comment stripper) | test_double_dash_inside_column_name_is_not_a_comment | FAIL |
| R2-8 | CRITICAL | `rollback(work, target)` onto an existing non-model folder deletes it (user data lost); no check that target is a model | test_rollback_refuses_non_model_target | FAIL |
| R2-9 | HIGH | `VAR t = FILTER(...) RETURN COUNTROWS(t)` flagged "missing table t" -> valid measure planned for deletion | test_var_table_variable_is_not_a_missing_table | FAIL |
| R2-10 | HIGH | Unqualified column in row context `SUMX(FactClaims, [ReserveAmount])` flagged missing -> measure deleted and column trimmed | test_unqualified_column_in_row_context_is_valid | FAIL |
| R2-11 | HIGH | UNTESTED bypass: an untranslatable measure whose DAX definition changed stays UNTESTED (def not compared on that path) | test_untested_definition_change_is_unexpected | FAIL |
| R2-12 | HIGH | UNTESTED bypass: an untested measure that is BROKEN after the trim is graded UNTESTED | test_untested_that_becomes_broken_is_unexpected | FAIL |
| R2-13 | HIGH | UNTESTED bypass: untested check missing from only one of the two after-runs is UNTESTED, not UNEXPECTED | test_untested_disappearing_in_only_one_run_is_unexpected | FAIL |
| R2-14 | HIGH | Bookmark filter using a `From`/`Source` alias (no Entity) is not resolved; its column is trimmed and the bookmark breaks | test_bookmark_alias_filter_column_kept | FAIL |
| R2-15 | HIGH | A broken measure referenced only from a visual calculation is deleted instead of blocked | test_visual_calculation_measure_ref_counts_as_use | FAIL |
| R2-16 | MEDIUM | Power Query partition step referencing a column is ignored, no refusal (listed in README limitations) | test_power_query_partition_column_kept_or_refused | FAIL |
| R2-17 | MEDIUM | `rollback()` silently deletes a pre-existing `<target>.smguard-restore` folder it did not create | test_rollback_does_not_delete_preexisting_staging_folder | FAIL |
| R2-18 | MEDIUM | Removing a column leaves its `///` description, which re-attaches to the next object | test_mid_file_removal_takes_its_doc_comment | FAIL |
| R2-19 | MEDIUM | TMDL file with a UTF-8 BOM: planned removals in it are not applied (header not matched); caught by `planned_columns_gone`, so not silent, but trim fails | test_removal_in_bom_file | FAIL |
| R2-20 | MEDIUM | summary.json has no explicit `untested` count next to `unexpected`; env reported `safe: true` with 4 UNTESTED checks | test_demo_not_safe_with_untested_checks | FAIL |
| R2-21 | LOW | README says "adversary suite: 36 tests"; repo now has more | test_readme_test_counts_match_repo | FAIL |

### Attacks that held (PASS today)
- work_dir refused when reached via `..`, different case, a relative path, a directory junction into the model, or a model passed through a junction.
- Mid-file removal in a CRLF file is byte-exact; previous column's annotation is kept.
- Nested parentheses + string literal containing `[Ghost]`; `//` inside a quoted table name; TREATAS / USERELATIONSHIP columns kept.
- Valid-before then broken-after is UNEXPECTED; bookmark filters bound by Entity are read; visual calculations do not produce false missing-field failures.
- README per-env classification prose (identical / expected / fresh data / UNTESTED) matches results/summary.json; README table still matches (Round 1 tests).

### Verdict Round 2: **NO-GO**
8 critical and 7 high findings are open. The builder's own limitation list covers R2-1..R2-4 and R2-16, but the
Round 2 rule is that columns used there must be kept or the trim must refuse with "unsupported feature present";
today it trims them and reports `safe: true`. R2-7 is a regression introduced by the Round 1 comment fix.

## Round 2 fixes (builder)

No adversary test or row was edited; no finding is disputed.

Design change: **safe by default.** The TMDL reader now records every line it does not understand
(`Model.unsupported`); `plan()` and `apply()` raise `UnsupportedFeature("unsupported feature present: ...")`
when that list is non-empty, and the CLI `trim` prints `safe: false` and exits 2. Unknown keyword = refusal.

| # | Fix |
|---|---|
| R2-1 | calculationGroup / calculationItem parsed; item DAX is a reference source. |
| R2-2 | definition/perspectives/*.tmdl parsed; perspectiveColumn pins its column. |
| R2-3 | OLS `columnPermission` under tablePermission pins its column. |
| R2-4 | Calculated partitions parsed; field-parameter `NAMEOF('T'[C])` keeps the column. |
| R2-5 | Calculated-table DAX source is a reference source. |
| R2-6 | `formatStringDefinition` parsed and counted with its measure (usage and break detection). |
| R2-7 | Comment stripper copies `[bracketed]`, `'quoted'` and `"string"` tokens verbatim. |
| R2-8 | `rollback()` requires the smguard marker in work_dir and only replaces a target that is absent or the exact model `apply()` backed up (path recorded in the marker). |
| R2-9 | `VAR` names are not table references. |
| R2-10 | Unqualified `[col]` resolves to a column of a table iterated in the expression (SUMX, FILTER, ...). |
| R2-11..13 | UNTESTED is UNTESTED only if present in both after-runs, not broken, and with an identical definition in all three runs; otherwise UNEXPECTED. |
| R2-14 | Bookmark `From` aliases are mapped so `SourceRef.Source` resolves to its Entity. |
| R2-15 | Measure refs inside visual calculations count as uses (broken -> BLOCK); a broken one also fails the consumer check. |
| R2-16 | Power Query (M) partitions parsed; any `[Name]` / `"Name"` in the M code keeps that column of the table. Any other partition kind is a refusal. |
| R2-17 | rollback refuses if `<target>.smguard-restore` already exists; it never deletes it. |
| R2-18 | A `///` doc comment is part of its object's block and is removed with it. |
| R2-19 | BOM on the first line is ignored for matching and preserved on write. |
| R2-20 | summary.json: `untested`, `untested_measures`, `untested_acknowledged`, `trim_safe`; `safe` requires 0 unexpected and every untested measure explicitly acknowledged (demo records one written acknowledgement). |
| R2-21 | README counts updated (21 builder, 71 adversary by the adversary's counting rule). |

Builder added 2 tests (unknown construct refuses; rollback onto a different model refuses).

## Round 3

Re-run first: **92 collected, 91 pass, 1 fail.** The one failure (`test_readme_test_counts_match_repo`) is
caused by the adversary: `test_adv_round3.py` was written while the run was in flight, so the README's adversary
count went stale mid-run. The builder's claim of 92/92 is accepted.

Round 3 tests: `tests/adversarial/test_adv_round3.py`, **31 tests, 15 FAIL, 16 PASS**. They use synthetic minimal
TMDL only and never re-run the demo; the file takes about 3 minutes, mostly inside `apply()`'s re-analysis.

| # | Sev | Finding | Test | Today |
|---|---|---|---|---|
| R3-1 | CRITICAL | Refusal bypass: `definition/model.tmdl` is skipped wholesale, so an RLS role written inline there is ignored and its column is trimmed | test_role_inline_in_model_tmdl_is_read_or_refused | FAIL |
| R3-2 | CRITICAL | Refusal bypass: `database.tmdl` is skipped the same way, so a calculated table inside it is ignored and its source column is trimmed | test_table_inline_in_database_tmdl_is_read_or_refused | FAIL |
| R3-3 | CRITICAL | `detailRowsDefinition` is parsed but then dropped, so a column used only for drill-through is trimmed with no refusal | test_detail_rows_definition_column_kept | FAIL |
| R3-4 | HIGH | Deleting a broken measure that a perspective lists (`perspectiveMeasure`) leaves a dangling reference, so the model is invalid | test_perspective_measure_on_broken_measure_not_deleted_silently | FAIL |
| R3-5 | HIGH | Rollback is not exact: a backup edited after `apply()` is restored without complaint, because there is no hash of the original in the marker | test_tampered_backup_is_detected_on_rollback | FAIL |
| R3-6 | HIGH | False positive in row context: `FILTER(ALL(FactClaims), [ReserveAmount] > 0)` is flagged broken, so a valid measure is planned for deletion | test_filter_over_all_table_unqualified_column_valid | FAIL |
| R3-7 | HIGH | False positive: `SUMX(DimPolicy, SUMX(RELATEDTABLE(FactClaims), [ReserveAmount]))` is flagged broken | test_sumx_over_relatedtable_unqualified_column_valid | FAIL |
| R3-8 | HIGH | False negative in scoping: `[ReserveAmount]` inside a DimPolicy iterator passes because FactClaims is iterated elsewhere in the expression | test_unqualified_column_out_of_scope_is_broken | FAIL |
| R3-9 | HIGH | False negative: `[ReserveAmount]` used after the SUMX has closed, with no row context, is accepted | test_unqualified_column_outside_any_iterator_is_broken | FAIL |
| R3-10 | HIGH | Ruling on the self-acknowledgement: **not acceptable.** The demo's sign-off for its own UNTESTED measure is hard-coded in `smguard/demo.py` (`ACKNOWLEDGED_UNTESTED`), so the tool marks its own homework and reports `safe: true`. The acknowledgement must live in a reviewed file outside the code (`results/acknowledgements.json`: measure, reviewer, date, reason), and the README must call it a human sign-off | test_untested_acknowledgement_is_not_hardcoded_in_code, test_untested_acknowledgement_is_a_reviewed_results_file | FAIL |
| R3-11 | MEDIUM | A VAR named like a table hides a missing-table reference that appears before the VAR is defined | test_var_shadowing_table_name_does_not_hide_missing_table | FAIL |
| R3-12 | MEDIUM | `apply()` wipes any folder that contains an empty `.smguard-work` file, even with user files in it; the marker content is never checked | test_foreign_marker_does_not_license_work_dir_deletion | FAIL |
| R3-13 | MEDIUM | False refusal: a standard calculation group with `precedence: 1` is refused. It is safe, but blocks real models | test_calc_group_precedence_property_not_false_refusal | FAIL |
| R3-14 | LOW | README adversary test count is stale again (now 102 by the counting rule) | test_readme_adversary_count_current | FAIL |

### Attacks that held (PASS today)
- **Refused or read correctly:**
  - Indentation tricks: space-indented column, tab+space property, property nested one level too deep, M code at the property depth.
  - Constructs: an unknown `kpi` object under a measure, a multi-line calculation item, a perspectiveHierarchy.
  - Quoted names and references: OLS on quoted names, unquoted `NAMEOF` in a multi-line field parameter, M code with `#"Claim Status"`.
- A formatStringDefinition that references a missing measure breaks its measure.
- **Marker handling:**
  - A tampered marker cannot redirect rollback onto another folder.
  - With the marker missing, or replaced by a directory, rollback refuses and leaves the target untouched.
- **Results and README:** acknowledged and untested sets agree in summary.json; the README table and UNTESTED counts match results/.

### Verdict Round 3: **NO-GO**
3 critical and 7 high findings are open. The "safe by default" refusal has a hole: whole files are exempt
(`model.tmdl`, `database.tmdl`), and a parsed property (`detailRowsDefinition`) is discarded without a refusal.
Row-context scoping is still a heuristic and fails in both directions. The UNTESTED sign-off must move out of the code.
None of the three mediums is in the README limitations yet.

## Round 3 fixes (builder)

No adversary test or row was edited (only the builder-owned "Fixes" / "Final state" cells of rows 2 and 3);
no finding is disputed. Orchestrator direction: stop patching, switch to a STRICT ALLOWLIST.

| # | Fix |
|---|---|
| Design | `smguard/tmdl.py` enforces an explicit allowlist (`SUPPORTED_FILES`, `CHILDREN`, `PROPS`, `EXPR_PROPS`): every file in the model folder, every object under its parent, every property, tab-only indentation one level per step. `smguard/dax.py` adds a DAX function allowlist (`DAX_FUNCTIONS`). Anything else = `UnsupportedFeature` (CLI exit 2). README documents the subset as a table. |
| R3-1, R3-2 | Any file outside the four allowlisted patterns (incl. `model.tmdl`, `database.tmdl`) is a refusal. |
| R3-3 | `detailRowsDefinition` is read as a reference source of its measure (with formatStringDefinition). |
| R3-4 | A broken measure listed in a perspective is BLOCKED, not deleted. |
| R3-5 | The marker stores a SHA-256 manifest of the original; rollback verifies the backup (and the staging copy) against it before touching the target. |
| R3-6..9 | New token-based DAX analyser with a frame stack: bare `[col]` resolves only in the ACTIVE row context. Iterator over a bare table = certain (resolved or broken); over ALL()/RELATEDTABLE()/FILTER()/VAR/other = uncertain (column kept, not broken). Outside any iterator = broken. |
| R3-10 | Sign-off moved to `results/acknowledgements.json` (measure, reviewer, date, reason); incomplete records are ignored. README calls it a human sign-off and says the demo entry is a placeholder signed by the author. |
| R3-11 | VAR names only hide table references that appear after the VAR is defined. |
| R3-12 | apply() only wipes a non-empty work folder whose marker is valid smguard JSON with a UUID4 signature, model path and manifest. |
| R3-13 | `precedence` (and `description`) allowlisted on calculationGroup. |
| R3-14 | README counts updated (23 builder, 102 adversary). |
| Speed | apply() loads each model once, computes column stats once and reuses them for the after-model, plus a per-parquet-version stats cache. |

Builder added 2 tests (DAX function outside the allowlist refuses; an incomplete acknowledgement is not a sign-off).
Known limit, now in README: the marker is not tamper-proof against someone who can rewrite the whole work folder.

## Round 4

Re-run first: every test the builder counted passes. The only 2 failures are the README test-count checks
(`test_readme_test_counts_match_repo`, `test_readme_adversary_count_current`). They went stale because
`test_adv_round4.py` was added during the run, so they are not the builder's fault. Builder claim of 125/125 accepted.

Round 4 tests: `tests/adversarial/test_adv_round4.py`, **31 test cases (one parametrised x9), 17 FAIL, 14 PASS,
about 50 s.**

| # | Sev | Finding | Test | Today |
|---|---|---|---|---|
| R4-1 | HIGH | **Refuses every real-world model.** A minimal PBIP skeleton is refused. The skeleton follows the public TMDL docs: `database.tmdl` with compatibilityLevel; `model.tmdl` with culture, defaultPowerBIDataSourceVersion, `ref table`, annotations and a queryGroup; `cultures/en-US.tmdl`. `.platform`, `definition.pbism` and `diagramLayout.json` are refused too. A safe tool that never runs on a real project is not a credible result | test_real_world_tmdl_skeleton_is_accepted, test_real_world_pbip_folder_with_metadata_files_is_accepted, test_real_world_skeleton_trim_still_removes_unused_column | FAIL |
| R4-2 | HIGH | The sign-off accepts placeholders: reviewer `TBD`, `PLACEHOLDER`, `n/a`, `-`, `x`, `reviewer`, `todo`, and a date of `someday`. Only an empty or blank reviewer is rejected | test_placeholder_reviewer_is_not_a_signoff[*], test_invalid_date_is_not_a_signoff | FAIL (8 cases) |
| R4-3 | HIGH | The committed `results/acknowledgements.json` says in its own reviewer field that it is a PLACEHOLDER, yet DEV/UAT/PROD are reported `safe: true` on it | test_committed_signoff_marked_placeholder_does_not_make_env_safe | FAIL |
| R4-4 | HIGH | dax.py: in an "uncertain" row context, a bare column falls back to any table that has that name. `SUMX(ALL(DimPolicy), [ReserveAmount])` is accepted although DimPolicy has no such column, so the break is hidden | test_uncertain_context_does_not_hide_out_of_scope_column | FAIL |
| R4-5 | HIGH | dax.py: VAR scope is global. A VAR defined inside an inner `( ... RETURN ... )` hides a missing-table reference outside it | test_inner_var_scope_does_not_leak | FAIL |
| R4-6 | MEDIUM | dax.py: an unquoted non-ASCII table name (`Schäden[PaidAmount]`) is tokenised as `den[...]`, so a valid measure is reported broken | test_unicode_unquoted_table_name | FAIL |
| R4-7 | MEDIUM | Forged marker (builder-listed open limit): copying a genuine marker into a folder of user files lets `apply()` wipe it, and rewriting backup + manifest + uuid together lets `rollback()` overwrite the model with forged content | test_copied_marker_does_not_license_wiping_user_folder, test_fully_forged_marker_and_backup_cannot_overwrite_model | FAIL (listed in README limitations) |

### Attacks that held (PASS today)
- Shared-expression file, `ref table` pointing at a missing table, DAX UDF file and an inline role in model.tmdl are refused.
  **Caveat:** today these pass trivially, because every skeleton is refused. They become real guards once R4-1 is fixed.
- A non-ASCII bracketed column name resolves and is kept; a table named like a function (`'Values'`) resolves.
- `SUMX(FILTER(...), [col])` keeps both columns; `sum (` in lower case with a space is allowlisted.
- Non-allowlisted functions are refused after a comment, inside a calculation item, and when called as dotted UDFs (`Lib.Fn`).
- A VAR shadowing a table name does not hide an earlier missing column on the real table.

### Verdict Round 4: **NO-GO**
5 high findings are open (R4-1..R4-5). R4-1 is the usefulness blocker: the allowlist must cover the standard PBIP
skeleton (database/model/cultures files and the metadata files next to `definition/`), while still refusing
genuinely unsupported content inside them. R4-2/R4-3 mean the human sign-off can be satisfied by a placeholder.
Of the mediums, R4-7 is already in the README limitations. R4-6 is not.

## Round 4 fixes (builder)

No adversary test or adversary-owned text was edited (only the builder-owned "Fixes" / "Final state" cells);
no finding is disputed.

| # | Fix |
|---|---|
| R4-1 | Allowlist extended to the standard PBIP skeleton: `definition/database.tmdl` (compatibilityLevel...), `definition/model.tmdl` (model props, dataAccessOptions flags, queryGroup, annotations, `ref table/culture/role/perspective`), `definition/cultures/*.tmdl` (cultureInfo + linguisticMetadata), and the metadata files `.platform`, `definition.pbism`, `diagramLayout.json` (ignored). Still refused: inline roles/tables in model.tmdl, shared expressions, UDF files, any `ref` that points at nothing. The generator now emits this skeleton, so the demo runs on a real-shaped PBIP folder. |
| R4-2 | Sign-off validation (`signoff_problem`): reviewer >= 3 chars with a letter, not a placeholder (TBD, PLACEHOLDER, PENDING, n/a, -, x, reviewer, todo, ...); date must be ISO and not in the future. |
| R4-3 | The author-signed placeholder was withdrawn. `results/acknowledgements.json` holds an unsigned PENDING entry; summary.json now reports `safe: false`, `status: "pending human sign-off (4 untested)"`, `untested_signed_off: []`, and each record with `accepted` / `rejected_because`. The demo exits 3 (waiting for sign-off). README shows the trim as BLOCKED. |
| R4-4 | Uncertain row context resolves only against the identified table (ALL(T), RELATEDTABLE(T), FILTER(T, ...)); a missing column there = broken. Same-named columns elsewhere are used only when no table can be identified. |
| R4-5 | Every `(` opens a lexical frame; a VAR is visible only until its frame closes. |
| R4-6 | Identifiers are tokenised as Unicode letters, so `Schäden[PaidAmount]` resolves. |
| R4-7 | Marker now also records its own work-folder path and is HMAC-signed with a per-user key outside the work folder; a copied marker (wrong folder) or a rewritten marker (bad MAC) is rejected, and `apply()` refuses to wipe a folder containing anything besides marker/backup/trimmed. |

Builder: 1 new test (sign-off rules), demo test updated to expect "pending human sign-off".

## Round 5

Re-run first, as asked, because the builder edited one of its own tests after its last full run:
**157 tests collected; everything passes except the 2 README test-count checks.** Those went stale because
`test_adv_round5.py` was added during the run. The builder's edited test passes inside the full suite.

Adversary housekeeping: in `test_adv_round2.py` the junction test and the case-difference test now SKIP off
Windows, instead of erroring on the Linux CI runner. Only adversary-owned code was touched.

Round 5 tests: `tests/adversarial/test_adv_round5.py`, **39 test cases, 20 FAIL, 19 PASS, about 30 s.**
Every key-handling test points `SMGUARD_KEY_DIR` at a temporary folder.

| # | Sev | Finding | Test | Today |
|---|---|---|---|---|
| R5-1 | HIGH | Usefulness gap: a real PBIP SemanticModel folder normally contains `.pbi/editorSettings.json` (committed by Desktop). It is refused as "file not in the supported subset", so real projects are still refused out of the box | test_skeleton_with_pbi_editor_settings_accepted | FAIL |
| R5-2 | HIGH | A tool or agent is accepted as the human reviewer: `smguard`, `Claude`, `Claude Code agent`. An agent could sign off its own work and the env would turn `safe: true`, which reopens the self-acknowledgement ruling of R3-10 | test_disguised_placeholder_or_tool_reviewer_rejected[smguard/Claude/Claude Code agent] | FAIL |
| R5-3 | HIGH | A sign-off is not bound to the definition it reviewed (no definition hash). Once given, it keeps covering the measure after its DAX changes in a later release, so a stale sign-off hides an unreviewed change | test_signoff_bound_to_definition | FAIL |
| R5-4 | MEDIUM | Disguised placeholders pass: `T.B.D.`, full-width `ＴＢＤ`, `P E N D I N G` | test_disguised_placeholder_or_tool_reviewer_rejected[T.B.D./ＴＢＤ/P E N D I N G] | FAIL |
| R5-5 | MEDIUM | An empty (0-byte) `marker.key` is used as-is, which makes every MAC forgeable | test_empty_key_file_refused | FAIL |
| R5-6 | MEDIUM | `SMGUARD_KEY_DIR` inside the work folder or inside a git repo is accepted, which defeats signing | test_key_dir_inside_work_dir_refused, test_key_dir_inside_git_repo_refused | FAIL |
| R5-7 | MEDIUM | Verifying a marker with the key missing silently mints a NEW key. That invalidates every other marker and destroys the evidence | test_rollback_does_not_silently_create_new_key | FAIL |
| R5-8 | MEDIUM | `ref` consistency is one-way: a duplicate `ref table` and a table file with no `ref` are both accepted | test_duplicate_ref_refused, test_table_file_without_ref_refused | FAIL |
| R5-9 | LOW | Absurd sign-off dates are accepted (`0001-01-01`, `1900-01-01`) | test_absurd_past_date_rejected[*] | FAIL |
| R5-10 | LOW | "Future date" is judged by the local date. A date that is already today in UTC+14 is rejected (false rejection, safe direction) | test_tomorrow_in_utc_plus_14_is_not_future_relative_to_reviewer | FAIL |
| R5-11 | LOW | With the key missing, rollback reports "forged or edited", which wrongly tells the user their backup was tampered with (it does refuse safely) | test_key_deleted_mid_flow_explains_cause | FAIL |
| R5-12 | LOW | `.gitignore` has no `marker.key` / `.smguard` pattern (the key lives in home by default; defence in depth) | test_gitignore_covers_key_and_work | FAIL |
| R5-13 | LOW | CI uses Python 3.12, but the brief mandates 3.13 | test_ci_python_matches_brief | FAIL |

### Attacks that held (PASS today)
- **Smuggling into model.tmdl and cultures files is refused:**
  - an inline table with a calculated partition;
  - an `expression` under `model`;
  - a role under a `queryGroup`;
  - a table or measure in a culture file;
  - culture `translations`;
  - an object hidden after a linguisticMetadata body or after a multi-line annotation.
- **Ref checks:** `ref culture` without a file, and `ref model Model` (self-reference), are refused. A case-only mismatch in `ref table` is accepted correctly.
- **Sign-off and key handling:**
  - Today's date is accepted; an ISO datetime with an offset is handled without crashing; `tbd!` is rejected.
  - Rollback with a deleted key refuses and leaves the target untouched.
  - No `marker.key` exists in the repo, and the builder's conftest isolates `SMGUARD_KEY_DIR`.

### CI note (exit code 3)
`.github/workflows/ci.yml` only runs `python -m pytest -q tests` for each project. **It never runs the demo, so
exit code 3 cannot fail CI.** No adjustment is needed. If a demo step is added later, it must accept exit 3
explicitly; `test_ci_does_not_treat_demo_exit_3_as_failure` checks this. Two separate CI risks:
- the runner is Linux with Python 3.12 (R5-13);
- the suite takes about 22 minutes, and the builder's own tests have not been checked on Linux.

### Verdict Round 5: **NO-GO**
3 high findings are open (R5-1..R5-3). R5-1 is the remaining usefulness gap. R5-2 and R5-3 mean the human
sign-off can still be given by an agent, or outlive the definition it reviewed. None of the mediums R5-4..R5-8
is in the README limitations yet. No "Final state" section is written, because the GO condition is not met.

## Round 5 fixes (builder)

No adversary test or adversary-owned text was edited (only the builder-owned cells of rows 4 and 5).

| # | Fix |
|---|---|
| R5-1 | `.pbi/editorSettings.json`, `.pbi/localSettings.json`, `.pbi/cache.abf` added to the ignored PBIP metadata list (public PBIP folder docs). |
| R5-2 | Reviewer is NFKC-normalised, case-folded, and punctuation and spaces are stripped; a tool/agent denylist (smguard, claude, agent, bot, ai, automated, gpt, copilot, assistant, script, ci, ...) is rejected; `reviewer_role: human` is required. |
| R5-3 | A sign-off must carry `definition_sha256`. `review_acknowledgements(path, model)` compares it with the SHA-256 of the measure's current normalised definition (expression + format + detail rows) in EACH environment; a mismatch = "definition changed since the sign-off". The shipped PENDING entry carries the current hash. |
| R5-4 | Disguised placeholders (T.B.D., full-width TBD, P E N D I N G, tbd!) are caught by the normalised compact form. |
| R5-5 | A key shorter than 32 bytes (incl. empty) is refused. |
| R5-6 | Key folder inside/overlapping the work folder, or inside any git repo, is refused before anything is touched. |
| R5-7, R5-11 | Only `apply()` may create a key. Verification raises `KeyMissing` ("smguard marker key missing ... restore the original key file"), never "forged". |
| R5-8 | Duplicate `ref`s refused; with a model.tmdl present, every table file must have a `ref table`. |
| R5-9, R5-10 | Dates before 2000-01-01 rejected; "future" means later than today in UTC+14. ISO datetimes are accepted as their date. |
| R5-12 | Project `.gitignore` covers `marker.key` and `.smguard/` (`work/` already). |
| R5-13 | Repo CI `python-version` set to 3.13 (`.github/workflows/ci.yml`). |
| Speed | Unchanged design: one session-scoped generated dataset and one session-scoped demo run shared by builder and adversary tests, and a stats cache; on this machine the full suite now takes about 6 minutes (it was 22 before Round 3). |

Builder: 1 new test (sign-off goes stale when the definition changes); 2 builder tests updated for the new required fields.

### Builder response (Round 5) - 3 tests left FAILING for adjudication

`test_today_is_accepted`, `test_datetime_with_timezone_handled` and
`test_tomorrow_in_utc_plus_14_is_not_future_relative_to_reviewer` build their record with only
`measure, reviewer, date, reason` and expect `signoff_problem()` to return None (valid). That contradicts
`test_signoff_bound_to_definition` (same four fields, expects rejection because there is no definition hash)
and the orchestrator's Round 5 direction (`reviewer_role: human` and a definition hash are REQUIRED). No
implementation can satisfy both. The date logic those three tests target IS implemented (UTC+14 future bound,
ISO datetime accepted as its date, today accepted). Proof: with `reviewer_role: "human"` and a 64-hex
`definition_sha256` added to the record, all three return None. Suggested adjudication: the adversary adds those
two fields to the base record in `_problem()`. The builder did not edit the tests.

## Round 6

**Adjudication applied.** Following the orchestrator's ruling, the adversary added `reviewer_role: "human"` and
`definition_sha256` to the base record of `_problem()` in `test_adv_round5.py`. Those fields were the
adversary's own Round 5 demands, so the three date tests were self-contradictory. They now pass and test only
the date logic. **Round 5: 39/39 pass.**

**Full suite: 234 tests, 220 pass, 14 fail.** 12 of the failures are new Round 6 tests. The other 2 are the
README test-count checks, which are stale only because `test_adv_round6.py` was added.

Round 6 tests: `tests/adversarial/test_adv_round6.py`, **38 test cases, 12 FAIL, 26 PASS, about 40 s.**

**Canonicalisation decision (adversary ruling, encoded in the tests):**
- Whitespace or comment changes OUTSIDE string literals and identifiers are not a definition change; the sign-off stays valid.
- Any change INSIDE a string literal, a `[name]` or a `'quoted table'` IS a definition change.
- A change to any measure the signed measure depends on IS a definition change.

| # | Sev | Finding | Test | Today |
|---|---|---|---|---|
| R6-1 | HIGH | Stale sign-off survives an identifier change: `normalise()` deletes whitespace inside `'quoted tables'` and `[names]`, so `'Fact Claims'[PaidAmount]` and `'FactClaims'[PaidAmount]` (different tables) get the SAME hash, as do `[Paid Amount]` and `[PaidAmount]` | test_change_inside_literal_or_identifier_is_stale[quoted table / bracket name] | FAIL |
| R6-2 | HIGH | Stale sign-off survives a dependency change: the hash covers only the measure's own text. `M = [Base] * 2` stays signed off after `[Base]` changes, although what M computes changed. Untranslatable measures are exactly the ones nobody can regression-test, so this is where it matters | test_dependency_change_makes_signoff_stale | FAIL |
| R6-3 | MEDIUM | Homoglyph and look-alike tool names pass the reviewer check: Cyrillic `Сlaude`, `Clаude`, small-capital `ᴄlaude`, script-g `smɡuard`, `C1aude`. (Zero-width space, soft hyphen and combining marks ARE caught.) Rated MEDIUM, not HIGH, because a name check cannot authenticate anyone: a dishonest signer can type any human name. The real control is the human review process. The README should say the denylist is a speed bump, not authentication | test_homoglyph_tool_names_rejected[5 cases] | FAIL |
| R6-4 | MEDIUM | False rejection of real human names: `李明` ("too short", 2 characters) and `Claudette Scott` ("tool or agent", substring match on `claude`) | test_real_human_names_accepted[李明], test_names_containing_tool_substrings_accepted[Claudette Scott] | FAIL |
| R6-5 | MEDIUM | Ref checks are bidirectional only for tables. A `roles/*.tmdl` file with no `ref role`, or a `cultures/*.tmdl` file with no `ref culture`, is still accepted | test_role_file_without_ref_role_refused, test_culture_file_without_ref_refused | FAIL |

### Attacks that held (PASS today)
- **Reviewer check:**
  - Zero-width space, soft hyphen and combining strike-through inside `Claude` are rejected.
  - Ordinary names are accepted (incl. `Zoë Ndlovu`, `Siobhán O'Brien`, `Aidan Botha`, `Bridget Clinton`).
  - `reviewer_role` tolerates case and spaces, but rejects homoglyph or invisible variants of `human`.
- **Definition hash:**
  - Whitespace- and comment-only edits keep the hash.
  - A changed string literal, or a case change inside one, changes it; so does a `formatStringDefinition` change.
  - An upper-case or space-padded hash in the file still matches.
- **Ref checks:**
  - A case-only duplicate `ref table` is refused.
  - A role WITH its `ref role` is accepted.
  - A `ref table` that names a file but not the table inside it is refused.
- **Key folder:**
  - Refused inside a repo marked by a `.git` FILE (worktree or submodule), via a relative path, and through a junction.
  - Accepted outside any repo, and the key is at least 32 bytes.

### Verdict Round 6: **NO-GO**
2 high findings are open (R6-1, R6-2); both let a human sign-off outlive the definition it reviewed. The fix:
- canonicalise by tokens, keeping string, bracket and quoted-name tokens verbatim, so only whitespace BETWEEN tokens is dropped;
- hash the measure together with the hashes of every measure it depends on, transitively.

The mediums R6-3..R6-5 are not yet in the README limitations. Because the GO condition is not met, the
"Final state" section is not written yet; it will be written in the round that reaches GO.

## Round 6 fixes (builder)

No adversary test or adversary-owned text was edited (only the builder-owned cells of rows 5 and 6).

| # | Fix |
|---|---|
| R6-1 | `definition_hash` is token-canonical: the DAX is tokenised (smguard/dax.py), comments are dropped, and tokens are joined by one space. Strings, `[names]` and `'quoted tables'` are single tokens kept byte-exact, so changes inside them change the hash, while whitespace/comment-only edits between tokens do not. |
| R6-2 | The hash is transitive: it covers `[measure]=canonical` for the measure and every measure it depends on (resolved via the model, recursively, sorted). `Measure.model` is a back-reference set by `load_model`, so `definition_hash(measure)` needs no extra argument. The shipped PENDING entry carries the new hash. |
| R6-3 | Before the tool-name check, invisible format characters and combining marks are removed and common look-alikes are folded (Cyrillic and Greek letters, small capitals, script g, 1/0/3/5/4/7). README states the denylist is a speed bump, not authentication; a PR review of acknowledgements.json is the real control. |
| R6-4 | Tool names match only as WHOLE words (or the whole compacted name), so `Claudette Scott` passes; the length rule is now at least 2 letters in any script, so `李明` passes. |
| R6-5 | Ref checks are bidirectional for roles and cultures as well: every `role` in roles/*.tmdl and every `cultureInfo` in cultures/*.tmdl needs a matching `ref` in model.tmdl (matched by object name, not file name). |
| Counts | README: 25 builder tests, 206 adversary tests. |

## Round 7

Re-run first: **234/234 builder-counted tests pass.** The only 2 failures in the run are the README test-count
checks, which went stale because `test_adv_round7.py` was added during the run. Builder claim confirmed: every
Round 6 test now passes.

Round 7 tests: `tests/adversarial/test_adv_round7.py`, **14 test cases, 2 FAIL, 12 PASS, about 50 s.**
Scope: only the two Round 6 hash changes (token-canonical and transitive).

| # | Sev | Finding | Test | Today |
|---|---|---|---|---|
| R7-1 | HIGH | The transitive hash follows MEASURE references only. `A = CALCULATE([Total Paid], 'Time Calc'[Name] = "H1")` applies calculation item H1 explicitly, but changing H1's DAX leaves A's hash unchanged, so a stale sign-off survives. This is the same class as R6-2 | test_measure_filtering_on_calc_item_goes_stale_when_item_changes | FAIL |
| R7-2 | HIGH | Same gap for calculated columns: `A = SUM(FactClaims[Net])`, and changing Net's DAX (`-` to `+`) leaves A's hash unchanged | test_measure_on_calculated_column_goes_stale_when_column_changes | FAIL |

### Attacks that held (PASS today)
- **Cycles:**
  - A -> B -> A and self-reference hash without hanging.
  - Changing any member of a 3-cycle makes the others stale.
- **Hash stability:**
  - Unrelated measures and file order do not change the hash (no false stale).
  - Renaming a dependency does change it.
  - Calculation groups present in the model do not break hashing.
- **Performance:**
  - The top of a 1,500-deep chain hashes in about 1.4 s; a 1,000-way fan-in hashes quickly.
  - A bottom change propagates 300 levels.
  - `plan()` on a 1,500-deep chain takes about 7 s.

### Lows / strict-xfail
No LOW finding is open, so nothing was marked strict-xfail. Every LOW from Rounds 1-5 is closed.

### Verdict Round 7: **NO-GO**
2 high findings are open. Both are the last gap in "a sign-off goes stale when what it reviewed changes": the
transitive hash must also include calculated columns the measure reads (and THEIR dependencies), and calculation
items referenced through their group's column. A conservative alternative is to include every calculation item
in the model whenever the measure references a calculation-group table. This is a small, contained fix. The
"Final state" section is not written, because the GO condition (no open critical or high) is not met.

## Round 7 fixes (builder)

No adversary test or adversary-owned text was edited (only the builder-owned cells of rows 6 and 7).

| # | Fix |
|---|---|
| R7-1 | `definition_hash` walks a graph of measures AND calculated columns. Whenever a visited node touches a calculation-group table (a column of it or the table itself), ALL calculation items of that group (expression + format string, canonical) are added - conservative by design. |
| R7-2 | Calculated columns read by any visited node are hashed by their canonical DAX, and their own references (measures, columns, calc groups) are followed. |
| Hash form | Sorted `kind:name=canonical` parts (`measure:`, `column:`, `calcitem:`) preceded by `root:[signed measure]`; file order still does not matter, renames still change the hash, cycles terminate. |
| Entry | `results/acknowledgements.json` PENDING entry updated to the new hash (`b090638f...`). |
| README | Counts (25 builder, 220 adversary) and a "How this was built" section with the rounds table and the Round 3 strict-allowlist turning point. |

## Round 7 re-check

After the builder's fix (the hash now covers calculated columns transitively, plus every item of any calculation group a
measure touches): `test_adv_round7.py` **14/14 pass**, and the full suite is **248/248 pass, 0 fail**. The README
counts (25 builder, 220 adversary) match the suite, so the count checks are green. **R7-1 and R7-2 are closed.**

## Final state

**Verdict: GO.** No critical or high finding is open. The full suite is 248/248 green: 25 builder tests and 223
adversary test cases. The adversary count of 223 is by pytest collection; the README's 220 uses the adversary's
round-2 counting rule. No LOW is open, so no test is marked xfail.

| Round | Critical | High | Medium | Low | Total | Closed |
|---|---|---|---|---|---|---|
| 1 | 6 | 7 | 6 | 2 | 21 | 21 |
| 2 | 8 | 7 | 5 | 1 | 21 | 21 |
| 3 | 3 | 7 | 3 | 1 | 14 | 14 |
| 4 | 0 | 5 | 2 | 0 | 7 | 7 |
| 5 | 0 | 3 | 5 | 5 | 13 | 13 |
| 6 | 0 | 2 | 3 | 0 | 5 | 5 |
| 7 | 0 | 2 | 0 | 0 | 2 | 2 |
| **All** | **17** | **33** | **24** | **9** | **83** | **83** |

**Open mediums/lows: none as findings.** These design limits are accepted residual risk, each disclosed in README
"Limitations":
- Allowlist refuses common real content (shared expressions, UDFs, translations, KPIs, many DAX functions): README "Strict allowlist still refuses common real content".
- Row-context resolution is conservative, not exact: README "Row context is conservative, not exact".
- Power Query reading is loose: README "Power Query reading is loose".
- Anyone who can read the per-user key can forge a marker, and losing the key blocks rollback: README "Marker trust rests on a per-user key".
- The sign-off check is a speed bump, not authentication: README "Sign-off checks are rule-based"; a PR review of `results/acknowledgements.json` is required.
- The size figures are estimates, only reports in scope are checked, and UNTESTED measures need human sign-off: README first bullets.

**Lesson: the turning point was the strict-allowlist redesign in Round 3.** Rounds 1-3 produced 17 critical findings,
each a new construct the parser did not know about (RLS, hierarchies, calculated columns, partitions, model.tmdl, ...)
being trimmed while the tool reported `safe: true`. Each patch fixed one construct and the next round found another.
Once unknown content became a refusal instead of an assumption, criticals went to zero, and they stayed at zero for four
rounds. The remaining work moved from "it deletes things in use" to usefulness (Round 4: it refused every real
model) and to the integrity of the human sign-off (Rounds 5-7).
