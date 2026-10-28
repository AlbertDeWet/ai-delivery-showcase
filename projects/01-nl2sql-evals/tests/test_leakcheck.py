import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# The leak check lives once, at the monorepo root (scripts/leakcheck.py).
REPO = ROOT.parent.parent
spec = importlib.util.spec_from_file_location("leakcheck", REPO / "scripts" / "leakcheck.py")
leakcheck = importlib.util.module_from_spec(spec)
spec.loader.exec_module(leakcheck)

TERMS = ["acme secret project", "c:/private"]


def test_term_hit_case_insensitive():
    assert leakcheck.scan_text("notes on ACME Secret Project", TERMS)


def test_backslash_path_matches_forward_slash_term():
    assert leakcheck.scan_text(r"see C:\Private\file.txt", TERMS)


def test_json_escaped_backslash_path_matches():
    bs = chr(92)
    escaped = "C:" + bs * 2 + "Private" + bs * 2 + "file.txt"  # how json.dumps writes C:\Private\file.txt
    assert leakcheck.scan_text('{"path": "' + escaped + '"}', TERMS)
    assert leakcheck.scan_text("C://Private//file.txt", TERMS)
    assert leakcheck.scan_text("C:" + bs + "u005cPrivate", TERMS)


def test_email_flagged_but_example_allowed():
    planted = "mail someone" + "@" + "realcorp.io"  # assembled so this file passes its own check
    assert "email address" in leakcheck.scan_text(planted, TERMS)
    assert not leakcheck.scan_text("mail someone@example.com", TERMS)


def test_attribution_line_flagged():
    line = "Co" + "-Authored-By: Someone <x@example.com>"
    assert "attribution line" in leakcheck.scan_text(line, TERMS)


def test_clean_text_passes():
    assert leakcheck.scan_text("Contoso Insurance (fictional sample data)", TERMS) == []


def test_missing_denylist_fails_closed(tmp_path):
    assert leakcheck.main(["--denylist", str(tmp_path / "nope.txt"), "--paths", "README.md"]) == 2


def test_planted_file_blocked(tmp_path):
    deny = tmp_path / "deny.txt"
    deny.write_text("# comment\nacme secret project\n", encoding="utf-8")
    planted = ROOT / "tests" / "_planted_tmp.txt"
    planted.write_text("this mentions the Acme Secret Project", encoding="utf-8")
    try:
        assert leakcheck.main(["--denylist", str(deny), "--paths", str(planted)]) == 1
    finally:
        planted.unlink()


# ---- author identity (review item 5) ----

def test_identity_hit_on_denylisted_email():
    terms = ["owner" + "@" + "private-mail.test"]
    assert leakcheck.identity_hits(["Owner" + "@" + "Private-Mail.test"], terms)
    assert not leakcheck.identity_hits(["someone" + "@" + "users.noreply.github.com"], terms)


def test_identity_env_override_is_checked(tmp_path):
    env = {"GIT_AUTHOR_EMAIL": "a" + "@" + "x.test", "GIT_COMMITTER_EMAIL": "c" + "@" + "y.test"}
    emails = leakcheck.identity_emails(env)
    assert emails[:2] == [env["GIT_AUTHOR_EMAIL"], env["GIT_COMMITTER_EMAIL"]]


def test_commit_msg_blocked_when_author_email_denylisted(tmp_path, monkeypatch):
    bad = "owner" + "@" + "private-mail.test"
    deny = tmp_path / "deny.txt"
    deny.write_text(bad + "\n", encoding="utf-8")
    msg = tmp_path / "msg.txt"
    msg.write_text("Add guard tests\n", encoding="utf-8")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", bad)
    assert leakcheck.main(["--denylist", str(deny), "--commit-msg", str(msg)]) == 1
    monkeypatch.delenv("GIT_AUTHOR_EMAIL")
    monkeypatch.delenv("GIT_COMMITTER_EMAIL", raising=False)
    assert leakcheck.main(["--denylist", str(deny), "--commit-msg", str(msg)]) == 0


def test_repo_local_identity_is_safe():
    import subprocess

    email = subprocess.run(["git", "config", "--local", "--get", "user.email"], cwd=ROOT,
                           capture_output=True, text=True).stdout.strip()
    if not email:
        import pytest

        pytest.skip("no repo-local identity (fresh clone or CI)")
    deny = ROOT / "denylist.txt"
    terms = leakcheck.load_terms(deny) if deny.exists() else []
    assert email.endswith("@users.noreply.github.com")
    assert not leakcheck.identity_hits([email], terms)
