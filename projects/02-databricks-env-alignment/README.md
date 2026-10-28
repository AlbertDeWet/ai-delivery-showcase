# 02 - Databricks environment alignment (DEV / UAT / PROD)

Contoso Insurance (fictional sample data). This is a local simulation: no Databricks, no Spark, no cloud.

## Problem

On a consulting programme for a large financial-services client, three Databricks environments had drifted apart. The drift
covered views, config tables, notebook paths, job definitions and bundle targets. Nobody could say which
differences were deliberate (per-environment settings) and which were real drift. Changes had to go through Git
(feature branch -> PR to DEV -> PR to UAT -> PR to PROD). Another team's assets lived in the same workspaces and
had to stay untouched.

## Approach

Each "workspace" is a DuckDB file (views, data, `cfg_*` config tables, a protected `team_b` schema), a deployed
copy of a `databricks.yml` asset bundle with dev/uat/prod targets, and a folder of job JSON definitions.
`python -m envalign seed` builds all three with deliberate drift.

| Command | What it does |
|---|---|
| `python -m envalign diff --left dev --right prod` | `envdiff`: fingerprints normalised view DDL, config rows (hash per key), normalised job JSON and bundle sections. Prints an "X of N identical" matrix and classifies each difference as identical / expected per-environment / drift / missing / extra. Protected objects are reported separately. |
| `python -m envalign plan --source dev --target uat --split` | `promote`: writes a PR-sized markdown + JSON plan. Each item has a backup step, the change, pre-tests, post-tests and a rollback step. The risk linter flags **R1** `CREATE OR REPLACE`/`DROP` on an object any job uses (paused jobs included, since they can be resumed), **R2** any touch on the protected namespace; any SQL outside an allowlist decided on DuckDB's own parse tree (`extract_statements` + `json_serialize_sql`): one plain CREATE VIEW/TABLE, INSERT, DELETE or DROP on the item's own object; only `core`/`_backup`/CTE tables (no files, globs or table functions); only allow-listed functions; anything DuckDB cannot serialise, and any comment, `$` or `E'` string, is rejected; a change to an object the protected team depends on, even transitively; and any change while a protected-team job declares no dependencies (all blocking), **R3** a missing or fake backup/rollback (blocking), **R4** config keys being removed or a table rebuilt, and **R5** a change that overwrites a target-only version (e.g. a PROD-only hotfix). R1 follows view dependencies transitively and covers scheduled, continuous and untagged running jobs. `--split` moves R1 items into their own PR. |
| `python -m envalign apply --plan P --env uat` | Dry run by default. `--execute --approve <token>` runs it only when the token matches an entry in the approvals ledger (kept outside the plan file and signed with an HMAC under a local, gitignored secret) that covers both this exact plan content and the target job definitions/tags and views the linter read, and that has not been used before (approvals are single use), there are no blocking findings, every HIGH finding has its own `--accept-risk RULE:ITEM` (e.g. `R1:004`), and the target has not changed since planning. It then takes a full file backup with a SHA-256 register, runs the baseline tests, applies the change in one transaction with per-item backup tables, and runs the same tests again. If anything fails, it restores the backup automatically and exactly: files the change created are removed and read-only files are handled. If a file cannot be restored, it reports `ROLLBACK_PARTIAL` with per-file errors and the backup location, and never claims success. |
| `python -m envalign demo` | The full story, end to end. Writes everything to `results/`. |

Design choices:
- Missing views are created with `CREATE VIEW IF NOT EXISTS`. `CREATE OR REPLACE` on a view that a 30-minute job
  uses is flagged R1 and split into its own PR.
- Views and tables that exist only in the target are reported, never dropped.
- Acceptance tests: T1 every core view returns rows without error; T2 each changed object's fingerprint equals
  the source; T3 the protected team's fingerprint is unchanged; T4 data-table row counts are unchanged; T5 every
  object a job depends on exists.

## How the AI method was applied

This project was built by directing Claude Code agents. A builder agent (Opus, low thinking) wrote the code and
the builder tests. A separate adversarial agent then attacks it and writes its own tests in
`tests/adversarial/`. Its rounds are logged in `ADVERSARIAL-LOG.md`. Round 1 returned NO-GO with 25 of 39
adversary tests failing (forgeable approval token, protected-namespace bypass via quoting, missed indirect job
dependencies, silent PROD hotfix overwrite, inexact rollback, and more). Round 2 returned NO-GO with 15 of 60 failing (an
approval token that could be forged from the plan file, lint inputs not bound to the approval, dynamic SQL,
reverse dependencies of the protected team, and rollback crashing on read-only files). Round 3 returned NO-GO
with 11 of 87 failing (dynamic SQL spelled with comments or quoted names, untagged protected-team jobs, approval
replay). Round 4 returned NO-GO with 8 failing: a hand-written SQL tokenizer could be desynchronised from DuckDB's
(`$$` strings, nested comments), and `FROM '<file>'` read local files. The guard now decides on DuckDB's own parse
tree. H4 remains a documented limitation (strict xfail in the adversary suite). See the log.

## Results (from `results/demo-summary.md`, one recorded run)

Alignment before and after the demo promotions (DEV->UAT, then UAT->PROD):

| pair | type | before identical | before aligned* | after identical | after aligned* |
|---|---|---|---|---|---|
| dev->prod | view | 32 of 39 | 32 of 39 | 38 of 39 | 38 of 39 |
| dev->prod | config_table | 20 of 30 | 21 of 30 | 29 of 30 | 30 of 30 |
| dev->prod | job | 0 of 5 | 3 of 5 | 0 of 5 | 5 of 5 |
| dev->prod | bundle | 5 of 7 | 5 of 7 | 7 of 7 | 7 of 7 |
| dev->uat | view | 36 of 38 | 36 of 38 | 38 of 38 | 38 of 38 |
| dev->uat | config_table | 27 of 30 | 28 of 30 | 29 of 30 | 30 of 30 |

\*aligned = identical + expected per-environment difference. Jobs are never byte-identical across environments
because their notebook paths, pause state and cluster size are environment-specific. The view that stays
unaligned in PROD is a PROD-only debug view: it is reported, not dropped.

- The gate refused the DEV->UAT R1 PR (a `CREATE OR REPLACE` on a view used by the 30-minute job) until the risk
  was accepted explicitly for that item. The UAT->PROD promotion ran as two PRs: 15 items, then 3 R1 items. Every
  item that overwrote a PROD-only version was flagged R5 and needed its own acceptance (scripted in the demo).
- Protected team: their fingerprint showed 0 differences before vs after in all three environments.
- Rollback drill: a planted view that fails at query time failed T1, and the environment was restored
  automatically. The restore was verified against the backup register.
- Builder tests: 35 passed (`results/builder-tests.txt`). Adversary tests: 135 passed, 3 xfailed (strict: H4, K1, K2) (`results/adversarial-tests.txt`)
  after the Round 2 fixes.
- Demo elapsed time: 74.5 s on this machine (`results/demo-summary.md`, re-run after the seed currency change). Other work was competing for the CPU
  during that run, so treat the number as an upper bound, not a benchmark. The demo exits non-zero if any apply
  ends in an unexpected status (3 for `ROLLBACK_PARTIAL`).

## How this was built

The project was built by directing Claude Code agents. A builder agent (Opus, low effort) wrote the code and its
own tests. A separate adversarial agent attacked the work in rounds and wrote its own tests in
`tests/adversarial/`, and the builder iterated until the adversary declared GO. Every round is in
`ADVERSARIAL-LOG.md`.

| Round | Adversary tests | Failed on first run | Severe findings (critical / high) |
|---|---|---|---|
| 1 | 39 | 25 | 1 / 6: forgeable approval token, protected-namespace bypass, missed job dependencies, PROD hotfix overwrite, inexact rollback |
| 2 | 60 | 15 | 1 / 4: token forgeable from the plan file, lint inputs not bound, dynamic SQL, protected reverse dependencies, rollback on read-only files |
| 3 | 87 | 11 | 0 / 2: regex dynamic-SQL check bypassed by comments and quoting, untagged protected-team jobs |
| 4 | 111 | 8 | 1 / 1: hand-written tokenizer desynchronised from DuckDB (`$$`, nested comments), file reads via `FROM '<path>'` |
| 5 | 138 | 2 | 0 / 0: two low findings (K1, K2), now documented: GO |

The lesson: home-made SQL parsing failed twice. A regex blocklist (round 2 fix) and then a hand-written tokenizer
(round 3 fix) were each bypassed in the next round, because their lexing disagreed with DuckDB's. The guard only
held once the security decision moved to DuckDB's own parser (`extract_statements` and `json_serialize_sql`).

## Limitations / open risks

- K1 (low): a single quoted identifier that contains a dot (`CREATE VIEW "core.v8"`) is accepted as the item's
  object `core.v8`, but DuckDB creates `main."core.v8"`, which is outside the managed schema. It cannot reach the
  protected team. Pinned by a strict-xfail adversary test.
- K2 (low): CTE names are collected across the whole parse tree, not per scope, so an unqualified name used
  outside its CTE's scope is accepted. DuckDB binds such a name to the view's own schema, never to `team_b`.
  Pinned by a strict-xfail adversary test.

- The SQL guard fails closed. Statements with comments, `$` or `E'...'` strings, UPDATE, or a function that is not on
  `ALLOWED_FUNCTIONS` in `envalign/sqlguard.py` are blocked even when harmless. Widening it is a reviewed code
  change.

- R1 starts from each job's `uses_objects` tag and follows the view-dependency graph transitively; a running job
  with no tag makes every replace "impact unknown". Notebook code that reads objects without a tag is invisible.
- R2 parses quoted, backticked and bracketed identifiers and USE/SET statements, and blocks dynamic SQL
  (`EXECUTE IMMEDIATE`, `query()`). Dynamic SQL inside notebooks (outside the plan) is not visible to it.
- The approvals ledger and its HMAC secret are local files. Anyone with write access to `.envalign/secret.key`
  can mint entries, so in real use the secret would live in a vault and approvals would be recorded by a
  separate identity. The ledger records that a plan was issued for review, not that a human reviewed it:
  `write_plan` signs any plan it is handed, including an edited one (adversary finding H4, still open). The human approval is the person
  typing the token and the per-item acceptances.
- Environment paths are recognised only in the bundle deployment form (`/bundles/<name>/<env>/`), plus catalog
  names, hosts and on-call mailboxes. Any other per-environment path convention would need adding.
- The "full backup" is a file copy. On Delta it would be a table version for time travel. Restoring a file
  copy also overwrites any concurrent writes made during the change window.
- Promotion overwrites target rows/views with the source. R5 flags target-only versions, but deciding to
  back-port a hotfix instead is still a human call.
- Expected per-environment fields are a fixed allow-list in `normalise.py`. An unlisted per-environment field
  shows up as drift.
- The scripted demo supplies its own approval tokens and per-item acceptances. In real use a human types them.
- Runtime on this Windows machine is dominated by DuckDB file open/close (seconds each).
