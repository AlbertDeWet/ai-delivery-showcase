"""ADVERSARY round 7 (final): attack on the two Round 6 hash changes only -
token-canonical hashing and transitive dependency hashing: cycles, calculation items,
calculated columns, very deep chains. Fast: no demo runs."""
import time

import pytest
from helpers import FACT, make_model, make_reports

from smguard.demo import definition_hash
from smguard.tmdl import load_model
from smguard.trim import plan

VIS = (("Measure", "FactClaims", "Total Paid"),)


def _model(tmp_path, measures="", extra=None, fact=FACT):
    return load_model(make_model(tmp_path, fact=fact + measures, extra=extra))


# ---------- cycles ----------

def test_cyclic_measures_hash_terminates(tmp_path):
    """ADVERSARY: A -> B -> A (circular) does not hang or crash the transitive hash."""
    m = _model(tmp_path, "\n\tmeasure A = [B] + 1\n\n\tmeasure B = [A] + 1\n")
    t = time.perf_counter()
    h = definition_hash(m.measures["A"])
    assert len(h) == 64 and time.perf_counter() - t < 2


def test_cycle_member_change_makes_stale(tmp_path):
    """ADVERSARY: in a cycle A -> B -> C -> A, changing C changes A's hash."""
    a = _model(tmp_path / "a", "\n\tmeasure A = [B]\n\n\tmeasure B = [C]\n\n\tmeasure C = [A] + 1\n")
    b = _model(tmp_path / "b", "\n\tmeasure A = [B]\n\n\tmeasure B = [C]\n\n\tmeasure C = [A] + 2\n")
    assert definition_hash(a.measures["A"]) != definition_hash(b.measures["A"])


def test_self_reference_hash_terminates(tmp_path):
    """ADVERSARY: X = [X] hashes without recursion."""
    m = _model(tmp_path, "\n\tmeasure X = [X]\n")
    assert len(definition_hash(m.measures["X"])) == 64


def test_hash_independent_of_unrelated_measure(tmp_path):
    """ADVERSARY: changing a measure that A does NOT depend on leaves A's hash unchanged (no false stale)."""
    a = _model(tmp_path / "a", "\n\tmeasure A = [Total Paid]\n\n\tmeasure Z = 1\n")
    b = _model(tmp_path / "b", "\n\tmeasure A = [Total Paid]\n\n\tmeasure Z = 2\n")
    assert definition_hash(a.measures["A"]) == definition_hash(b.measures["A"])


def test_hash_independent_of_measure_order(tmp_path):
    """ADVERSARY: the same definitions in a different file order give the same hash."""
    a = _model(tmp_path / "a", "\n\tmeasure A = [B] + [C]\n\n\tmeasure B = 1\n\n\tmeasure C = 2\n")
    b = _model(tmp_path / "b", "\n\tmeasure C = 2\n\n\tmeasure B = 1\n\n\tmeasure A = [B] + [C]\n")
    assert definition_hash(a.measures["A"]) == definition_hash(b.measures["A"])


def test_dependency_rename_makes_stale(tmp_path):
    """ADVERSARY: A = [B]; renaming B to B2 (and A to use it) with identical text changes A's hash."""
    a = _model(tmp_path / "a", "\n\tmeasure A = [B]\n\n\tmeasure B = 1\n")
    b = _model(tmp_path / "b", "\n\tmeasure A = [B2]\n\n\tmeasure B2 = 1\n")
    assert definition_hash(a.measures["A"]) != definition_hash(b.measures["A"])


# ---------- calculation items and calculated columns ----------

CG = ("table 'Time Calc'\n\tcalculationGroup\n\n\t\tcalculationItem H1 = CALCULATE(SELECTEDMEASURE(), DimDate[Month] <= {m})\n\n"
      "\tcolumn Name\n\t\tdataType: string\n\t\tsourceColumn: Name\n")


def test_measure_filtering_on_calc_item_goes_stale_when_item_changes(tmp_path):
    """ADVERSARY: A = CALCULATE([Total Paid], 'Time Calc'[Name] = "H1") explicitly applies calc item H1;
    when H1's DAX changes, A computes something else -> A's hash must change."""
    meas = "\n\tmeasure A = CALCULATE([Total Paid], 'Time Calc'[Name] = \"H1\")\n"
    a = _model(tmp_path / "a", meas, extra={"definition/tables/Time Calc.tmdl": CG.format(m=6)})
    b = _model(tmp_path / "b", meas, extra={"definition/tables/Time Calc.tmdl": CG.format(m=3)})
    assert definition_hash(a.measures["A"]) != definition_hash(b.measures["A"])


def test_measure_on_calculated_column_goes_stale_when_column_changes(tmp_path):
    """ADVERSARY: A = SUM(FactClaims[Net]) where Net is a calculated column; changing Net's DAX changes what A computes."""
    def fact(expr):
        return FACT + f"\n\tcolumn Net = {expr}\n\t\tdataType: double\n"
    meas = "\n\tmeasure A = SUM(FactClaims[Net])\n"
    a = _model(tmp_path / "a", meas, fact=fact("FactClaims[PaidAmount] - FactClaims[ReserveAmount]"))
    b = _model(tmp_path / "b", meas, fact=fact("FactClaims[PaidAmount] + FactClaims[ReserveAmount]"))
    assert definition_hash(a.measures["A"]) != definition_hash(b.measures["A"])


def test_selectedmeasure_inside_calc_item_does_not_crash_hash(tmp_path):
    """ADVERSARY: a model with calculation items (SELECTEDMEASURE) does not break hashing of normal measures."""
    m = _model(tmp_path, extra={"definition/tables/Time Calc.tmdl": CG.format(m=6)})
    assert len(definition_hash(m.measures["Total Paid"])) == 64


# ---------- very deep chains (performance) ----------

def _chain(n):
    s = "\n\tmeasure M0 = SUM(FactClaims[PaidAmount])\n"
    for i in range(1, n):
        s += f"\n\tmeasure M{i} = [M{i - 1}] + 1\n"
    return s


@pytest.mark.parametrize("n", [300, 1500])
def test_deep_chain_hash_is_fast(tmp_path, n):
    """ADVERSARY: hashing the top of an n-deep measure chain is fast and does not hit recursion limits."""
    m = _model(tmp_path, _chain(n))
    t = time.perf_counter()
    definition_hash(m.measures[f"M{n - 1}"])
    assert time.perf_counter() - t < 5


def test_deep_chain_bottom_change_makes_top_stale(tmp_path):
    """ADVERSARY: changing the bottom of a 300-deep chain changes the top's hash."""
    a = _model(tmp_path / "a", _chain(300))
    b = _model(tmp_path / "b", _chain(300).replace("SUM(FactClaims[PaidAmount])", "SUM(FactClaims[ReserveAmount])"))
    assert definition_hash(a.measures["M299"]) != definition_hash(b.measures["M299"])


def test_deep_chain_plan_is_fast(tmp_path):
    """ADVERSARY: plan() on a 1500-deep chain completes quickly (no quadratic blow-up in break detection)."""
    m = make_model(tmp_path, fact=FACT + _chain(1500))
    t = time.perf_counter()
    plan(m, make_reports(tmp_path, fields=VIS))
    assert time.perf_counter() - t < 20


def test_wide_fanin_hash_is_fast(tmp_path):
    """ADVERSARY: one measure summing 1000 others hashes quickly."""
    s = "".join(f"\n\tmeasure W{i} = {i}\n" for i in range(1000))
    s += "\n\tmeasure Top = " + " + ".join(f"[W{i}]" for i in range(1000)) + "\n"
    m = _model(tmp_path, s)
    t = time.perf_counter()
    definition_hash(m.measures["Top"])
    assert time.perf_counter() - t < 5
