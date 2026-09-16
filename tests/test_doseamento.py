from datetime import date
import unittest

from db.doseamento import calculate_doseamento


def rotation(consumption=10, issues=None):
    return {
        "stores": [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}],
        "intervals": [{
            "store": "A", "days": 1, "usable": not issues,
            "start_date": date(2025, 1, 1), "end_date": date(2025, 1, 2),
            "issues": issues or [], "consumption_kg": consumption,
            "opening_kg": 12, "production_kg": 2, "inbound_kg": 1,
            "outbound_kg": 0, "breakage_kg": 0, "closing_kg": 5,
        }],
    }


def sale(product, quantity=10, store="A", day=date(2025, 1, 1), weight=None):
    return {"produto": product, "quantidade": quantity, "loja": store,
            "data": day, "valor_euros": 20, "peso_vendido_kg": weight}


def test_fixed_formula_and_longest_mapping():
    result = calculate_doseamento(
        [sale("Copo Grande", 10)],
        [{"artigo": "Copo", "gramas": 100, "tipo_dose": "fixed"},
         {"artigo": "Copo Grande", "gramas": 250, "tipo_dose": "fixed"}],
        rotation(),
        date(2025, 1, 1), date(2025, 1, 1), "A",
    )
    assert result["theoretical_kg"] == 2.5
    assert result["variance_kg"] == 7.5
    assert result["yield_pct"] == 25.0
    assert result["status"] == "reliable"


def test_history_date_and_unmapped():
    result = calculate_doseamento(
        [sale("Cone", 1), sale("Unknown", 1)],
        [{"artigo": "Cone", "gramas": 100, "tipo_dose": "fixed",
          "valid_from": date(2025, 2, 1)}],
        rotation(), date(2025, 1, 1), date(2025, 1, 1), "A",
    )
    assert result["unmapped_products"] == ["Cone", "Unknown"]
    assert result["status"] == "incomplete"
    assert result["theoretical_kg"] is None


def test_weight_is_explicitly_incomplete():
    result = calculate_doseamento(
        [sale("Gelado weight")],
        [{"artigo": "Gelado", "gramas": 100, "tipo_dose": "weight"}],
        rotation(), date(2025, 1, 1), date(2025, 1, 1), "A",
    )
    assert result["weighted_products"] == ["Gelado weight"]
    assert "weight_products" in result["issues"]


def test_weight_with_kg_is_added_to_fixed_doses_without_double_counting():
    result = calculate_doseamento(
        [sale("Copo", 2, weight=9), sale("Gelado weight", 1, weight=1.25)],
        [{"artigo": "Copo", "gramas": 100, "tipo_dose": "fixed"},
         {"artigo": "Gelado", "gramas": None, "tipo_dose": "weight"}],
        rotation(), date(2025, 1, 1), date(2025, 1, 1), "A",
    )
    assert result["theoretical_kg"] == 1.45
    assert result["weighted_products"] == []
    assert "weight_products" not in result["issues"]


def test_weight_return_subtracts_from_theoretical_consumption():
    result = calculate_doseamento(
        [sale("Gelado weight", -1, weight=-0.4)],
        [{"artigo": "Gelado", "gramas": None, "tipo_dose": "weight"}],
        rotation(), date(2025, 1, 1), date(2025, 1, 1), "A",
    )
    assert result["mapped_theoretical_kg"] == -0.4


def test_global_supports_period_with_only_weight_sales():
    result = calculate_doseamento(
        [sale("Gelado weight", 1, weight=1.2)],
        [{"artigo": "Gelado", "gramas": None, "tipo_dose": "weight"}],
        rotation(), date(2025, 1, 1), date(2025, 1, 1),
        store_names=["A"],
    )
    assert result["status"] == "reliable"
    assert result["theoretical_kg"] == 1.2


def test_invalid_rotation_cannot_be_reliable():
    result = calculate_doseamento(
        [sale("Copo")], [{"artigo": "Copo", "gramas": 100}],
        rotation(10, ["negative_stock_residual"]),
        date(2025, 1, 1), date(2025, 1, 1), "A",
    )
    assert result["status"] == "invalid"
    assert result["real_kg"] is None
    assert result["variance_kg"] is None


def test_missing_sales_is_unknown_not_zero_kpi():
    result = calculate_doseamento(
        [], [{"artigo": "Copo", "gramas": 100}],
        rotation(), date(2025, 1, 1), date(2025, 1, 1), "A",
    )
    assert result["status"] == "incomplete"
    assert result["theoretical_kg"] is None
    assert result["real_kg"] is None
    assert result["yield_pct"] is None


def test_one_snapshot_flavor_makes_store_incomplete():
    evidence = rotation()
    evidence["rows"] = [{
        "sabor": "Fragola",
        "stores": {"A": {
            "snapshot_count": 1, "valid_intervals": 0,
            "excluded_intervals": 0, "coverage_pct": 0,
        }},
    }]
    result = calculate_doseamento(
        [sale("Copo")], [{"artigo": "Copo", "gramas": 100}],
        evidence, date(2025, 1, 1), date(2025, 1, 1), "A",
    )
    assert result["status"] == "incomplete"
    assert result["real_kg"] is None


def test_activity_without_snapshots_makes_store_incomplete():
    evidence = rotation()
    evidence["rows"] = [{
        "sabor": "Pistacchio",
        "stores": {"A": {
            "snapshot_count": 0, "activity_count": 1,
            "valid_intervals": 0, "excluded_intervals": 0,
            "coverage_pct": 0,
        }},
    }]
    result = calculate_doseamento(
        [sale("Copo")], [{"artigo": "Copo", "gramas": 100}],
        evidence, date(2025, 1, 1), date(2025, 1, 1), "A",
    )
    assert result["status"] == "incomplete"
    assert result["real_kg"] is None


def test_end_of_day_interval_covers_its_end_date():
    evidence = rotation()
    interval = evidence["intervals"][0]
    interval.update({
        "snapshot_type": "fim",
        "start_date": date(2024, 12, 31),
        "end_date": date(2025, 1, 1),
        "days": 1,
    })
    result = calculate_doseamento(
        [sale("Copo")], [{"artigo": "Copo", "gramas": 100}],
        evidence, date(2025, 1, 1), date(2025, 1, 1), "A",
    )
    assert result["status"] == "reliable"
    assert result["coverage_pct"] == 100.0


def test_global_ignores_stores_outside_explicit_scope():
    evidence = rotation()
    evidence["stores"].append({"id": 3, "name": "Unrelated"})
    result = calculate_doseamento(
        [sale("Copo")], [{"artigo": "Copo", "gramas": 100}],
        evidence, date(2025, 1, 1), date(2025, 1, 1),
        store_names=["A"],
    )
    assert result["status"] == "reliable"
    assert [row["loja"] for row in result["stores"]] == ["A"]


def test_global_preserves_mapped_subtotal_when_incomplete():
    evidence = rotation()
    result = calculate_doseamento(
        [sale("Copo"), sale("Unknown")],
        [{"artigo": "Copo", "gramas": 100}],
        evidence, date(2025, 1, 1), date(2025, 1, 1),
        store_names=["A"],
    )
    assert result["status"] == "incomplete"
    assert result["theoretical_kg"] is None
    assert result["mapped_theoretical_kg"] == 1.0


def test_global_conserves_store_totals():
    evidence = rotation()
    second = dict(evidence["intervals"][0])
    second.update({"store": "B", "consumption_kg": 20})
    evidence["intervals"].append(second)
    result = calculate_doseamento(
        [sale("Copo", 1, "A"), sale("Copo", 2, "B")],
        [{"artigo": "Copo", "gramas": 100, "tipo_dose": "fixed"}],
        evidence, date(2025, 1, 1), date(2025, 1, 1),
    )
    assert round(result["theoretical_kg"], 6) == round(sum(
        row["theoretical_kg"] for row in result["stores"]
    ), 6)
    assert result["revenue"] == sum(row["revenue"] for row in result["stores"])


def load_tests(_loader, _tests, _pattern):
    suite = unittest.TestSuite()
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            suite.addTest(unittest.FunctionTestCase(value))
    return suite