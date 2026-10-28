from envalign.envdiff import compare, matrix, snapshot
from envalign.workspace import workspaces


def _diff(root, l, r):
    ws = workspaces(root)
    d = compare(snapshot(ws[l]), snapshot(ws[r]))
    return {(x.kind, x.obj): x for x in d}, matrix(d)


def test_dev_uat_seeded_drift_is_found(root):
    d, m = _diff(root, "dev", "uat")
    assert d[("view", "core.v_broker_new")].status == "missing"
    assert d[("view", "core.v_claim_aging")].status == "drift"
    assert d[("config_table", "core.cfg_fx_source")].status == "drift"
    assert d[("job", "fx_load")].status == "drift"
    assert m["view"]["drift"] == 1 and m["view"]["missing"] == 1


def test_cosmetic_view_change_is_identical(root):
    d, _ = _diff(root, "dev", "uat")
    assert d[("view", "core.v_lapse_rate")].status == "identical"


def test_env_specific_differences_are_expected_not_drift(root):
    d, _ = _diff(root, "dev", "prod")
    assert d[("config_table", "core.cfg_environment")].status == "expected"
    assert d[("job", "policy_daily")].status == "expected"  # paused/workers/paths only


def test_prod_drift_kinds(root):
    d, m = _diff(root, "dev", "prod")
    assert d[("config_table", "core.cfg_channel")].status == "drift"  # schema drift
    assert d[("config_table", "core.cfg_feature_flags")].status == "missing"
    assert d[("view", "core.v_tmp_debug_claims")].status == "extra"
    assert d[("job", "claims_daily")].status == "drift"
    assert d[("bundle", "targets.prod")].status == "drift"
    assert m["bundle"]["drift"] == 2


def test_protected_objects_excluded_from_matrix(root):
    d, m = _diff(root, "dev", "prod")
    prot = [x for x in d.values() if x.protected]
    assert {x.obj for x in prot} == {"team_b.v_team_b_summary", "team_b.cfg_team_b_settings", "team_b_ingest"}
    total = sum(m[k]["total"] for k in ("view", "config_table", "job"))
    assert total == sum(1 for x in d.values() if not x.protected and x.kind != "bundle")
