import duckdb

from nl2sql import synth


def test_same_seed_is_identical():
    assert synth.fingerprint(synth.generate(7)) == synth.fingerprint(synth.generate(7))


def test_different_seed_differs():
    assert synth.fingerprint(synth.generate(7)) != synth.fingerprint(synth.generate(8))


def test_default_seed_is_stable_across_calls():
    a, b = synth.generate(), synth.generate()
    for name in a:
        assert a[name].equals(b[name]), name


def test_all_six_tables_present():
    assert set(synth.generate()) == {"policies", "customers", "products", "claims", "premiums", "brokers"}


def test_trap_mixed_case_statuses():
    t = synth.generate()
    pol = set(t["policies"]["status"])
    assert {"Active", "ACTIVE", "active"} <= pol
    clm = set(t["claims"]["status"])
    assert {"Open", "OPEN", "open"} <= clm


def test_trap_negative_reversals():
    pr = synth.generate()["premiums"]
    rev = pr[pr["transaction_type"] == "REVERSAL"]
    assert len(rev) > 0 and (rev["amount"] < 0).all()
    assert (pr[pr["transaction_type"] == "WRITTEN"]["amount"] > 0).all()


def test_trap_nulls():
    t = synth.generate()
    assert t["customers"]["region"].isna().any()
    assert t["policies"]["broker_id"].isna().any()
    assert t["claims"]["paid_amount"].isna().any()


def test_trap_fiscal_year_boundary_rows(db_path):
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        n = con.execute("SELECT COUNT(*) FROM premiums WHERE transaction_date IN "
                        "(DATE '2024-06-30', DATE '2024-07-01')").fetchone()[0]
    finally:
        con.close()
    assert n >= 2


def test_trap_injection_text_in_data():
    desc = synth.generate()["claims"].set_index("claim_id").loc[8, "description"]
    assert "IGNORE ALL PREVIOUS INSTRUCTIONS" in desc


def test_open_claims_have_null_paid():
    c = synth.generate()["claims"]
    open_ = c[c["status"].str.lower() == "open"]
    assert open_["paid_amount"].isna().all()


def test_database_build_row_counts(db_path):
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("brokers", "products", "customers", "policies", "premiums", "claims")}
    finally:
        con.close()
    gen = synth.generate()
    assert counts == {k: len(v) for k, v in gen.items()}
