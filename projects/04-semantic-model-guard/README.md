# 04 - Semantic model guard

Analyze, trim and regression-test Power BI semantic models in TMDL text format, across DEV/UAT/PROD,
without breaking a single consuming report. Everything runs locally on synthetic data for
**Contoso Insurance (fictional sample data)**.

## Problem

The shape comes from real work at a large financial-services client: a semantic model grew past its capacity
memory limit and refreshes started failing. The main fact table carried dozens of columns nobody used,
dozens of measures were broken, and DEV, UAT and PROD had drifted apart. Trimming a model is easy;
proving that the trim broke nothing (no measure, no relationship, no report visual, no number) is the hard part.

## Approach

`smguard` works on TMDL folders (the Power BI project text format) and PBIR-style report JSON:

1. **generate** - three synthetic models (DEV/UAT/PROD) with planted problems: a 32-column fact table
   where most columns are unused, high-cardinality text columns (narratives, hashes, GUID-like refs),
   10 broken measures (missing column, missing measure, missing table, transitive breaks), drift between
   environments, and 3 reports with 10 visuals.
2. **analyze** - parses TMDL, builds the measure dependency graph (finds direct, transitive and circular
   breaks), finds columns not referenced by any valid measure, relationship or report visual, and
   **estimates** size per column (dictionary + bit-packed index from the synthetic data's cardinality and
   type). The size is an estimate, not an engine measurement.
3. **trim --plan / --apply** - proposes removals with a per-object safety proof (0 measure refs, 0
   relationship refs, 0 visual refs). A broken measure that a visual uses is *blocked*, not deleted.
   `--apply` writes a backup copy, applies the change to a separate copy (the original is never touched),
   re-runs the analysis and checks: planned objects gone, everything else intact, no new broken measures,
   all reports pass, backup byte-identical to original. `rollback()` restores from the backup.
4. **regression suite** - every measure is translated from a DAX subset to SQL and run in DuckDB, as a
   total and grouped by year, line of business and product segment (every dimension join is exercised),
   plus row counts per table. A measure whose definition changed is always UNEXPECTED; a measure the
   translator cannot evaluate is UNTESTED. The data is exposed only
   through the columns the model defines, so a removed column that something needed makes a query fail.
   Differences are classified by also running the *after* model on the *before* data: a change on frozen
   data is UNEXPECTED; a change that only appears with new data is EXPECTED (fresh data); tiny float
   differences are EXPECTED (noise).
5. **consumer check** - every field in every report visual must resolve, and no visual may use a broken measure.
6. **drift** - tables, columns, measures and measure expressions compared across the three environments.

## Run it

```
python -m venv .venv                          # Python 3.13
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m smguard demo          # writes results/
.venv\Scripts\python -m pytest                # builder + adversarial tests
```

Single steps: `python -m smguard generate | analyze DEV | trim DEV --plan | trim DEV --apply | regress DEV | consumers DEV | drift`.

## How this was built (the AI method)

Built by directing Claude Code agents. An orchestrator session handed this project to a builder agent
(Opus at low effort) with a written brief. The builder wrote the tool, its tests and this README. A separate
adversarial agent then attacked the work each round and wrote its OWN tests in `tests/adversarial/`. The
builder had to make them pass without editing them; when a test was believed wrong, the builder logged a
"Builder response" and left it failing for the orchestrator to rule on (this happened once, in Round 5, and was
resolved when the orchestrator ruled the three tests self-contradictory and the adversary added the missing
required fields to them).

| Round | Adversary tests added | Failed first | Findings | What the round exposed |
|---|---|---|---|---|
| 1 | 36 | 25 | 21 (6 critical) | trim could delete columns used by multi-line measures, filters, calculated columns, RLS, hierarchies, sort-by; apply/rollback could destroy the original |
| 2 | 35 | 21 | 21 (8 critical) | unsupported model features (calc groups, perspectives, OLS, field parameters) were trimmed through; UNTESTED could hide changes |
| 3 | 31 | 15 | 14 (3 critical) | whole files exempt from the refusal; heuristic row-context scoping; sign-off hard-coded in code |
| 4 | 31 | 17 | 7 (5 high) | the strict allowlist refused every real PBIP project; placeholder sign-offs accepted |
| 5 | 39 | 20 | 13 (3 high) | `.pbi/` settings refused; an agent could sign as "human"; sign-off not bound to the definition |
| 6 | 38 | 12 | 5 (2 high) | definition hash blind to identifier changes and to upstream measures |
| 7 | 14 | 2 | 2 (2 high) | definition hash blind to calculated columns and calculation items |

**Turning point (Round 3):** after three rounds of patching individual holes, the orchestrator stopped the
whack-a-mole and switched the design to a STRICT ALLOWLIST: every file, object, property and DAX function must
be on an explicit list (see "Supported subset" below), anything else makes the trim refuse. Later rounds then
widened the list deliberately (the real PBIP skeleton) instead of discovering new bypasses. Full details per
round, including each fix, are in `ADVERSARIAL-LOG.md`.

The tool itself encodes the guarantees the method demands: backup before change, change applied to a copy,
the same tests before and after, every difference classified, and a human sign-off for what cannot be tested.

## Results

From `results/summary.md` (run of `python -m smguard demo`). MB figures are **estimates**.
PROD had a simulated refresh of 2,000 extra claims between the before and after runs.

| Env | Est. MB before | Est. MB after | Fact cols | Columns | Measures | Broken measures | Reports passing | Regression checks | Unexpected | Untested | Status |
|---|---|---|---|---|---|---|---|---|---|---|---|
| DEV | 32.889 | 1.535 | 32 -> 7 | 43 -> 17 | 26 -> 16 | 10 -> 0 | 3/3 | 108 | 0 | 4 | pending human sign-off (4 untested) |
| UAT | 48.732 | 2.102 | 33 -> 7 | 44 -> 17 | 24 -> 16 | 8 -> 0 | 3/3 | 100 | 0 | 4 | pending human sign-off (4 untested) |
| PROD | 64.651 | 2.659 | 32 -> 7 | 43 -> 17 | 25 -> 16 | 9 -> 0 | 3/3 | 104 | 0 | 4 | pending human sign-off (4 untested) |

**The trim is BLOCKED in all three environments until a human signs off.** Every automatic check passed
(`trim_safe: true`, 0 unexpected), but 4 regression checks per environment could not be evaluated, so
`summary.json` reports `safe: false` with `status: "pending human sign-off (4 untested)"`, and
`python -m smguard demo` exits with code 3 (0 = safe, 1 = blocked by a problem, 3 = waiting for sign-off).

Regression classification:
- DEV: 64 identical, 40 expected (checks of measures that were broken before and removed), 4 UNTESTED.
- UAT: 64 identical, 32 expected (same reason), 4 UNTESTED.
- PROD: 14 identical, 50 expected as fresh data (same definition, unchanged on frozen data, changed only by the refresh), 36 expected (broken before), 4 UNTESTED.

UNTESTED = the valid measure `[Avg Net per Claim]` uses `AVERAGEX`, which the regression translator does not
support. It is reported as untested, never as passed, and the columns it uses are kept. Any definition change,
break or disappearance of an untested measure is UNEXPECTED. `summary.json` carries an explicit `untested`
count per environment. An environment is only `safe: true` when every UNTESTED measure has a valid **human
sign-off** in the reviewed file `results/acknowledgements.json` (measure, definition_sha256, reviewer,
reviewer_role, date, reason) - never in code. A sign-off is rejected if:
- the reviewer, after Unicode NFKC normalisation, removal of invisible characters and combining marks, and
  stripping punctuation and spaces, is a placeholder (TBD, T.B.D., full-width TBD, P E N D I N G, n/a, x,
  reviewer, todo, ...); has fewer than 2 letters (any script, so `李明` is fine); or - after folding common
  look-alikes (Cyrillic/Greek letters, small capitals, `1`->`l`, `0`->`o`) - contains a tool or agent name as a
  WHOLE word (smguard, Claude, agent, bot, AI, automated, ...; `Claudette Scott` is fine);
- `reviewer_role` is not `human`;
- the date is not an ISO date, is before 2000-01-01, or is later than today anywhere on Earth (UTC+14);
- `definition_sha256` does not match the measure's CURRENT definition hash in that environment. The hash is
  token-canonical (comments dropped and whitespace collapsed only BETWEEN tokens; strings, `[names]` and
  `'quoted tables'` kept byte-exact) and transitive: it includes every measure the signed measure depends on,
  every calculated column it reads (and their dependencies) and, conservatively, ALL calculation items of any
  calculation group it touches. A sign-off goes stale as soon as any of these changes.

**The reviewer checks are a speed bump, not authentication.** Anyone can type a human-looking name. Real
assurance comes from process: changes to `results/acknowledgements.json` go through a pull-request review by
someone other than the author.
The shipped file deliberately holds an **unsigned PENDING entry** (the earlier author-signed placeholder was
withdrawn): this showcase does not mark its own homework, so the honest result is "pending human sign-off".

After the trim all three environments have the same table, column and measure counts (4 / 17 / 16) and 0 broken
measures. Drift items fell from 5 to 1: `[Avg Paid per Claim]` has a different expression in PROD
(same result, different text). The tool reports it; it does not auto-align expressions.

Builder test suite: 25 tests in `tests/`; adversary suite: 220 tests in `tests/adversarial/` (Rounds 1-7); all passing after Round 7 fixes.

## Limitations

- **Size is an estimate.** A simple dictionary + bit-packing model from synthetic stats; no real VertiPaq
  compression, hierarchies, relationships indexes or segment effects.
- **Regression translator is a DAX subset.** Measures outside it (e.g. `AVERAGEX`) are UNTESTED and need a
  human sign-off; they are never reported as passed.
- **Strict allowlist still refuses common real content.** The standard PBIP skeleton is accepted, but shared
  expressions (`expressions.tmdl`, e.g. parameters), DAX user-defined functions, translations in cultures,
  KPIs, calculation-item format strings beyond the listed ones and many DAX functions (e.g. OFFSET, WINDOW)
  are refused until they are read fully as reference sources.
- **Row context is conservative, not exact.** A bare `[col]` in an iterator over a table expression
  (ALL(T), RELATEDTABLE(T), FILTER(T, ...)) resolves against T only; if T lacks the column the measure is
  broken. Only when no table can be identified (e.g. a VAR or a complex expression) is any same-named column
  kept and the measure not flagged. VAR scope is lexical (to the end of the enclosing parentheses).
- **Power Query reading is loose.** Any `[Name]` or `"Name"` in M code keeps that column of the table.
- **Marker trust rests on a per-user key.** The work-folder marker (UUID, model path, work-folder path,
  SHA-256 manifest) is signed with an HMAC key (`~/.smguard/marker.key`, or `SMGUARD_KEY_DIR`). The key is
  created only by `apply()`, must be at least 32 bytes, and its folder may not be inside the work folder or
  any git repository. Verification never creates a key: a missing key is reported as "key missing", not as
  tampering. Someone who can read that key can still forge a marker, and losing the key makes old markers
  unverifiable (rollback then refuses).
- **Model.tmdl consistency is checked both ways**: every `ref` must point at an existing object, no `ref` may
  repeat, and every table, role and culture defined in its own file must have a matching `ref` in model.tmdl.
- **Sign-off checks are rule-based (speed bump, not authentication).** They block obvious placeholders and
  tool names, but cannot prove that a real, independent person signed; that needs a PR review of
  `results/acknowledgements.json`. The look-alike folding covers common Cyrillic/Greek/small-capital/digit
  substitutes only, not the full Unicode confusables table, and can in rare cases reject a real name whose
  folded form is a denylisted word (e.g. a reviewer named "Ai").
- **Non-ASCII names** are supported in quoted and unquoted identifiers (Unicode letters), but not tested
  against every script or right-to-left text.
- **Reports in scope only.** Only the report files in `reports/` are checked. Reports or Excel/Analyze-in-Excel
  consumers outside the folder are invisible - in real use the consumer list must be complete first.
- `FactClaims[ClaimKey]` is removed because nothing references it; a real team may still want a surrogate key.
- The "fresh data" rule relies on re-running the after-model on frozen before-data; it needs that snapshot to exist.
- Report bindings are found by walking every JSON field reference (projections, filters, bookmarks with
  From/Source aliases, measure refs in visual calculations); nothing else in report JSON is interpreted.

## Supported subset (strict allowlist)

Anything not in this table makes `trim` refuse with "unsupported feature present" (exit code 2).

| Level | Allowed | Read as a reference source |
|---|---|---|
| Files | `definition/database.tmdl`, `definition/model.tmdl`, `definition/cultures/*.tmdl`, `definition/tables/*.tmdl`, `definition/relationships.tmdl`, `definition/roles/*.tmdl`, `definition/perspectives/*.tmdl` | tables, relationships, roles, perspectives; any other file in the model folder (e.g. `expressions.tmdl`, `functions.tmdl`) = refuse |
| Metadata (ignored) | `.platform`, `definition.pbism`, `diagramLayout.json`, `.pbi/editorSettings.json`, `.pbi/localSettings.json`, `.pbi/cache.abf` (PBIP folder docs) | read-only, carry no references |
| database.tmdl | `database [name]`; props compatibilityLevel, compatibilityMode, language, collation | - (any child object = refuse) |
| model.tmdl | `model X` (props culture, defaultPowerBIDataSourceVersion, sourceQueryCulture, discourageImplicitMeasures, defaultMode, description; child dataAccessOptions with legacyRedirects / returnErrorValuesAsNull / fastCombine), `queryGroup`, `annotation`, `ref table/culture/role/perspective` | every `ref` must point at an existing object, else refuse; inline roles/tables/expressions = refuse |
| cultures/*.tmdl | `cultureInfo X` with `linguisticMetadata = ...` (contentType) | - (translations = refuse) |
| table | children: column, measure, hierarchy, partition, calculationGroup, annotation; props: lineageTag, isHidden, dataCategory, description | - |
| column | `column X` or calculated `column X = DAX`; props: dataType, sourceColumn, sortByColumn, formatString, summarizeBy, isHidden, lineageTag, dataCategory, displayFolder, isKey, isNullable, isNameInferred, isDataTypeInferred, isAvailableInMdx, description, sourceLineageTag | calculated expression (row context = own table), sortByColumn target |
| measure | `measure X = DAX` (single-line, indented multi-line, ``` fenced); props: formatString, displayFolder, lineageTag, isHidden, description; expression props: formatStringDefinition, detailRowsDefinition | expression, formatStringDefinition, detailRowsDefinition |
| hierarchy / level | level prop `column`, lineageTag, ordinal | level columns |
| calculationGroup / calculationItem | `precedence`, `description`, `ordinal`, formatStringDefinition | item DAX and format DAX |
| partition | `= calculated` or `= m`; props: mode, dataView, queryGroup; `source =` | calculated: DAX (calculated tables, field parameters with NAMEOF); m: any `[Name]`/`"Name"` |
| relationship | fromColumn, toColumn, crossFilteringBehavior, isActive, fromCardinality, toCardinality, securityFilteringBehavior, relyOnReferentialIntegrity, joinOnDateBehavior | both key columns |
| role | modelPermission, description; `tablePermission T = DAX`; `columnPermission C = ...` | RLS filter (row context = T), OLS column |
| perspective | perspectiveTable > perspectiveColumn / perspectiveMeasure / perspectiveHierarchy | columns, hierarchy level columns; a listed broken measure is BLOCKED, not deleted |
| annotation | wherever listed above | - |
| DAX functions | the list `DAX_FUNCTIONS` in `smguard/dax.py` (about 100 common functions) | any other function = refuse |
| Indentation | tabs only; one level per nesting step | spaces or unexpected nesting = refuse |
