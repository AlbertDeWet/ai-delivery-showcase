import json

import pytest

from smguard.analyze import report_fields
from smguard.regress import Untranslatable, classify, run_suite, translate
from smguard.tmdl import load_model, remove_objects
from smguard.trim import _same_tree, apply, plan, rollback


def test_plan_never_removes_referenced_columns(generated):
    p = plan(generated / "models" / "DEV", generated / "reports")
    removed = {(c["table"], c["column"]) for c in p["remove_columns"]}
    visual_cols = {(t, n) for _, _, k, t, n in report_fields(generated / "reports") if k == "column"}
    assert not removed & visual_cols
    assert ("FactClaims", "DateKey") not in removed          # relationship key
    assert ("FactClaims", "RecoveryAmount") not in removed   # used by a valid measure
    assert all(c["proof"] and set(c["proof"].values()) == {0} for c in p["remove_columns"])


def test_broken_measure_on_visual_is_blocked_not_removed(workspace):
    f = next((workspace / "reports").rglob("kpi_count/visual.json"))
    j = json.loads(f.read_text())
    j["visual"]["query"]["queryState"]["Values"]["projections"][0]["field"]["Measure"]["Property"] = "Broker Paid"
    f.write_text(json.dumps(j))
    p = plan(workspace / "models" / "DEV", workspace / "reports")
    assert [b["measure"] for b in p["blocked_measures"]] == ["Broker Paid"]
    assert "Broker Paid" not in {m["measure"] for m in p["remove_measures"]}


def test_apply_backs_up_and_leaves_original_untouched(generated, workspace):
    model = workspace / "models" / "UAT"
    snapshot = workspace / "snap"
    import shutil
    shutil.copytree(model, snapshot)
    p = plan(model, workspace / "reports")
    out = apply(model, generated / "data" / "UAT" / "v1", workspace / "reports", workspace / "work", p)
    assert out["safe"], out["checks"]
    assert _same_tree(model, snapshot)
    assert out["after"]["broken_measures"] == {}
    assert out["after"]["est_total_mb"] < out["before"]["est_total_mb"]
    target = workspace / "restored"
    rollback(workspace / "work", target)
    assert _same_tree(target, snapshot)


def test_translate_subset(generated):
    m = load_model(generated / "models" / "DEV")
    assert "NULLIF" in translate(m, "Avg Paid per Claim")
    assert "CASE WHEN" in translate(m, "Open Claims")
    with pytest.raises(Untranslatable):
        translate(m, "Paid Last Year")


def test_regression_catches_removed_used_column(generated, workspace):
    model = workspace / "models" / "DEV"
    data = generated / "data" / "DEV" / "v1"
    before = run_suite(model, data)
    remove_objects(model, {("FactClaims", "PaidAmount")}, set())
    after = run_suite(model, data)
    rows = classify(before, after, after)
    bad = {r["check"] for r in rows if r["class"] == "UNEXPECTED"}
    assert "total::Total Paid" in bad and "by_year::Net Paid" in bad


def test_regression_catches_broken_join(generated, workspace):
    model = workspace / "models" / "DEV"
    data = generated / "data" / "DEV" / "v1"
    before = run_suite(model, data)
    remove_objects(model, {("DimDate", "Year")}, set())
    after = run_suite(model, data)
    assert any(r["class"] == "UNEXPECTED" and r["check"].startswith("by_year::") for r in classify(before, after, after))


def test_classify_rules():
    b = {"x": {"value": 10.0}, "y": {"value": 1.0}, "z": {"error": "boom"}, "n": {"value": 1.0}}
    ao = {"x": {"value": 10.0}, "y": {"value": 1.5}, "n": {"value": 1.0 + 1e-13}}
    a = {"x": {"value": 12.0}, "y": {"value": 1.5}, "n": {"value": 1.0 + 1e-13}}
    got = {r["check"]: (r["class"], r["reason"]) for r in classify(b, ao, a)}
    assert got["x"][0] == "EXPECTED" and got["x"][1].startswith("fresh data")
    assert got["y"][0] == "UNEXPECTED"
    assert got["z"][0] == "UNEXPECTED"  # valid-but-unevaluable check vanished
    assert got["n"] == ("EXPECTED", "noise: float difference within tolerance")


def test_demo_end_to_end(demo_summary):
    root, s = demo_summary
    for env, e in s["envs"].items():
        assert e["trim_safe"] and e["unexpected"] == 0, env
        # the shipped sign-off file is deliberately unsigned -> trim blocked pending human review
        assert e["safe"] is False and e["status"] == f"pending human sign-off ({e['untested']} untested)", env
        assert e["broken_after"] == 0 and e["consumers_after"] == "3/3"
        assert e["est_mb_after"] < e["est_mb_before"]
    after = {(e["tables_after"], e["columns_after"], e["measures_after"]) for e in s["envs"].values()}
    assert len(after) == 1, "environments not aligned on counts"
    assert s["envs"]["PROD"]["regression"].get("EXPECTED: fresh data", 0) > 0
    assert all(e["regression"].get("UNTESTED", 0) > 0 for e in s["envs"].values())
    assert (root / "models" / "DEV" / "definition" / "model.tmdl").is_file()  # real PBIP skeleton accepted
    assert (root / "results" / "summary.md").exists()


def test_suite_joins_every_dimension(generated):
    r = run_suite(generated / "models" / "DEV", generated / "data" / "DEV" / "v1")
    assert {"by_year::Total Paid", "by_lob::Total Paid", "by_product::Total Paid"} <= set(r)
    assert all(v[1] is not None for v in r["by_product::Total Paid"]["value"])


def test_sort_by_target_kept_in_generated_model(generated):
    p = plan(generated / "models" / "DEV", generated / "reports")
    assert ("DimDate", "Month") not in {(c["table"], c["column"]) for c in p["remove_columns"]}


def test_unknown_tmdl_construct_refuses_trim(workspace):
    from smguard.trim import UnsupportedFeature
    f = workspace / "models" / "DEV" / "definition" / "tables" / "DimDate.tmdl"
    f.write_text(f.read_text(encoding="utf-8") + "\tmysteryObject Foo = 1\n", encoding="utf-8")
    with pytest.raises(UnsupportedFeature, match="unsupported feature present"):
        plan(workspace / "models" / "DEV", workspace / "reports")


def test_rollback_refuses_target_other_than_backed_up_model(generated, workspace, tmp_path):
    model = workspace / "models" / "DEV"
    apply(model, generated / "data" / "DEV" / "v1", workspace / "reports", tmp_path / "w",
          plan(model, workspace / "reports"))
    other = workspace / "models" / "UAT"
    with pytest.raises(ValueError):
        rollback(tmp_path / "w", other)
    assert (other / "definition").is_dir()


def test_dax_function_outside_allowlist_refuses_trim(workspace):
    from smguard.trim import UnsupportedFeature
    f = workspace / "models" / "DEV" / "definition" / "tables" / "FactClaims.tmdl"
    t = f.read_text(encoding="utf-8").replace("MAX(FactClaims[PaidAmount])", "PERCENTILEX.INC(FactClaims, FactClaims[PaidAmount], 0.9)")
    f.write_text(t, encoding="utf-8")
    with pytest.raises(UnsupportedFeature, match="DAX function not allowlisted"):
        plan(workspace / "models" / "DEV", workspace / "reports")


def test_incomplete_acknowledgement_is_not_a_sign_off(tmp_path):
    from smguard.demo import load_acknowledgements
    f = tmp_path / "a.json"
    f.write_text(json.dumps([{"measure": "M", "reviewer": "", "date": "2026-10-23", "reason": "x"},
                             {"measure": "N", "definition_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "reviewer_role": "human", "reviewer": "Jane Smith", "date": "2026-10-23", "reason": "y"}]))
    assert set(load_acknowledgements(f)) == {"N"}


def test_signoff_validation_rules(tmp_path):
    from smguard.demo import signoff_problem
    ok = {"measure": "M", "definition_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "reviewer_role": "human", "reviewer": "Jane Smith", "date": "2026-01-15", "reason": "read the TMDL"}
    assert signoff_problem(ok) is None
    assert signoff_problem({**ok, "reviewer": "PENDING - someone"}) == "placeholder reviewer"
    assert signoff_problem({**ok, "date": "2999-01-01"}) == "date is in the future"
    assert signoff_problem({**ok, "reviewer": "12"}) is not None


def test_signoff_rejected_when_definition_changed(generated, workspace, tmp_path):
    from smguard.demo import definition_hash, review_acknowledgements
    from smguard.tmdl import load_model
    m = load_model(workspace / "models" / "DEV")
    rec = {"measure": "Avg Net per Claim", "definition_sha256": definition_hash(m.measures["Avg Net per Claim"]),
           "reviewer": "Jane Smith", "reviewer_role": "human", "date": "2026-10-01", "reason": "read the TMDL"}
    f = tmp_path / "acks.json"
    f.write_text(json.dumps([rec]))
    assert review_acknowledgements(f, m)["Avg Net per Claim"]["accepted"]
    t = workspace / "models" / "DEV" / "definition" / "tables" / "FactClaims.tmdl"
    t.write_text(t.read_text(encoding="utf-8").replace("FactClaims[RecoveryAmount])", "FactClaims[RecoveryAmount] * 2)"),
                 encoding="utf-8")
    r = review_acknowledgements(f, load_model(workspace / "models" / "DEV"))["Avg Net per Claim"]
    assert not r["accepted"] and "definition changed" in r["rejected_because"]
