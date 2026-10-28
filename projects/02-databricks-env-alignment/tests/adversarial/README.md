# tests/adversarial/

This folder is filled by the **adversary** role, not by the builder.

The adversarial agent reads the code cold, tries to break `envdiff`, `promote` and `apply`, and writes its
own pytest files here. The builder then fixes the code until these tests pass. Every round is recorded in
`../../ADVERSARIAL-LOG.md`.

The builder must not edit or delete tests in this folder.
