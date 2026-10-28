# Real-world case studies, and the projects that re-create them

These cases come from client work for a large financial-services client, delivered with the
method in [README.md](README.md). They are described generically, by the shape of the problem
only: no client code, data, names, systems or figures appear in this repo. Each project under
`projects/` re-creates one shape on synthetic data ("Contoso Insurance (fictional sample data)"),
and only the numbers recorded in those projects are reported anywhere in this repo.

## 02 Environment alignment, and a NO-GO review

A branch meant to align development, test and production environments had bundled an urgent change
with riskier items: statements that rewrote shared views and configuration tables on every run, and
a business rule that had not yet been decided. The orchestrator gave the release to its own
sub-orchestrator, and builder agents prepared the branch. A separate adversarial review then
returned NO-GO for the bundle as it stood, because the rewrites were judged likely to break a core
scheduled job. That was a risk assessment, not a tested failure. The branch was split so the urgent
change could ship on its own, behind approval gates and a deployment check that had itself been
tested, and the risky items never reached production. Re-created in
[projects/02-databricks-env-alignment](../projects/02-databricks-env-alignment).

## 03 Data recovery and de-duplication

In a test environment, a manual run had removed a month of ledger data from the reporting layer.
The only line-level copy of the correct data was an old table version that time travel would soon
stop serving. A sub-orchestrator prepared everything read-only first: the plan, the backup, the fix,
the verification and the rollback scripts. Alternatives were simulated before one was chosen, and
the verification tests were dry-run to prove they detected the gap. The backup and the fix each
needed their own approval, and a rollback script was ready before anything ran. The data was
restored and reconciled to an independent summary before the deadline, with some downstream checks
still pending at the time. A related de-duplication fix went through feature branch, Dev and test
promotion, and another team's own query showed no differences. Re-created in
[projects/03-delta-pipeline-recovery](../projects/03-delta-pipeline-recovery).

## 04 Semantic model trim

A core Power BI semantic model had grown past its service size limit, and its scheduled refresh
had stopped. Worker agents analysed which content was unused or broken and what depended on it,
including security roles and consumer reports. The adversarial reviewer passed the analysis but
showed that the proposed rollback ("put the old copy back") would not work, so the plan changed to
fix-forward with verified backups. Each environment was changed only after its own typed approval,
with the same tests before and after. Refresh worked again, broken content was removed, and the
environments ended up structurally aligned. Some manual checks remained open at the time.
Re-created in [projects/04-semantic-model-guard](../projects/04-semantic-model-guard).

## 01 NL-to-SQL evals

This project does not re-create a single client case. It applies the same evaluation discipline as
the cases above (a fixed test set, a baseline, a before/after comparison, and an adversary writing
extra cases) to natural-language-to-SQL over the synthetic insurer data. Re-created in
[projects/01-nl2sql-evals](../projects/01-nl2sql-evals).
