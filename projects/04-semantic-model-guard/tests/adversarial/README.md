# Adversarial tests

This folder belongs to the ADVERSARY role. The builder does not write tests here.

The adversary agent tries to break `smguard` (parser, unused-column detection, trim safety proof,
regression classification, consumer check) and writes its own failing tests here. The builder then
iterates until they pass. Rounds and findings are recorded in `../../ADVERSARIAL-LOG.md`.

Status: empty - no adversarial round has run yet.
