import copy

from envalign.promote import build_plan, lint, plan_token, split, to_markdown


def test_every_item_has_backup_change_tests_rollback(root):
    plan = build_plan(root, "dev", "prod")
    assert plan["items"]
    for it in plan["items"]:
        assert it["backup"]["type"] and it["change"] and it["rollback"]
        assert it["pre_tests"] and it["post_tests"]


def test_protected_objects_never_planned_and_orphans_not_dropped(root):
    plan = build_plan(root, "dev", "prod")
    objs = {it["object"] for it in plan["items"]}
    assert not any(o.startswith("team_b") for o in objs)
    assert "core.v_tmp_debug_claims" not in objs
    assert any("v_tmp_debug_claims" in n for n in plan["notes"])
    assert not any("drop view" in s.lower() for it in plan["items"] for s in it["change"])


def test_missing_view_uses_if_not_exists(root):
    plan = build_plan(root, "dev", "uat")
    it = next(i for i in plan["items"] if i["object"] == "core.v_broker_new")
    assert it["change"][0].startswith("CREATE VIEW IF NOT EXISTS")


def test_r1_flags_replace_on_view_used_by_scheduled_job(root):
    plan = build_plan(root, "dev", "uat")
    f = lint(plan, root)
    r1 = [x for x in f if x["rule"] == "R1"]
    assert len(r1) == 1 and "core.v_claim_aging" in r1[0]["message"]
    a, b = split(plan, f)
    assert [i["object"] for i in b["items"]] == ["core.v_claim_aging"]
    assert not [x for x in lint(a, root) if x["rule"] == "R1"]


def test_r2_flags_cross_namespace_touch(root):
    plan = build_plan(root, "dev", "uat")
    bad = copy.deepcopy(plan)
    bad["items"][0]["change"].append("UPDATE team_b.cfg_team_b_settings SET value = 'x'")
    f = lint(bad, root)
    assert any(x["rule"] == "R2" and x["blocking"] for x in f)


def test_r3_flags_missing_backup(root):
    plan = build_plan(root, "dev", "uat")
    bad = copy.deepcopy(plan)
    bad["items"][0]["backup"] = None
    assert any(x["rule"] == "R3" and x["blocking"] for x in lint(bad, root))


def test_r4_flags_config_key_removal(root):
    plan = build_plan(root, "dev", "prod")
    f = lint(plan, root)
    assert any(x["rule"] == "R4" and "kpi_99" in x["message"] for x in f)


def test_token_is_bound_to_content(root):
    plan = build_plan(root, "dev", "uat")
    t = plan_token(plan)
    other = copy.deepcopy(plan)
    other["items"][0]["change"][0] += " "
    assert plan_token(other) != t
    assert t in to_markdown(plan, lint(plan, root))
