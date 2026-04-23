"""
Unit tests for the questionnaire-driven estimate engine.

Tests cover:
  - Impact merger logic (deterministic ordering, param accumulation)
  - Pakistan slab look-ups
  - Pakistan gray + finish line-item generation
  - Edge cases (empty answers, unknown option values)

Run with:
    bench --site <site> run-tests --app construction --module construction.tests.test_estimate_engine
"""

import unittest
from unittest.mock import MagicMock, patch

from construction.estimate_engine.engine import merge_impacts
from construction.estimate_engine.pk_calculator import (
    DRAWING_DEFAULT,
    DRAWING_SLABS,
    KAASOO_DEFAULT,
    KAASOO_SLABS,
    OTHER_DEFAULT,
    OTHER_SLABS,
    _calc_finish,
    _calc_gray,
    _item,
    _slab,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Minimal questionnaire data that mirrors the PK house_estimate_v1 seed
SAMPLE_QUESTIONNAIRE = {
    "steps": [
        {
            "step_key": "house_info",
            "order": 1,
            "questions": [
                {
                    "key": "structure_type",
                    "options": [
                        {"value": "gray", "impact": {"set_param": {"construction_type": "gray"}}},
                        {"value": "finished", "impact": {"set_param": {"construction_type": "both"}, "enable_phase": "finish"}},
                    ],
                },
                {
                    "key": "drawing_required",
                    "options": [
                        {"value": "yes", "impact": {"set_param": {"drawingRequired": True}}},
                        {"value": "no", "impact": {"set_param": {"drawingRequired": False}}},
                    ],
                },
            ],
        },
        {
            "step_key": "gray_structure",
            "order": 2,
            "questions": [
                {
                    "key": "brick_type",
                    "options": [
                        {"value": "awal_plus", "impact": {"set_param": {"ent_type": "Awal+"}}},
                        {"value": "awal", "impact": {"set_param": {"ent_type": "Awal"}}},
                        {"value": "dom", "impact": {"set_param": {"ent_type": "Dom"}}},
                    ],
                },
                {
                    "key": "foundation_depth",
                    "options": [
                        {"value": "3ft", "impact": {"set_param": {"foundation_type": "3 ft"}}},
                        {"value": "4ft", "impact": {"set_param": {"foundation_type": "4 ft"}}},
                    ],
                },
                {
                    "key": "termite_spray",
                    "options": [
                        {"value": "yes", "impact": {"set_param": {"termite_spray_required": True}}},
                        {"value": "no", "impact": {"set_param": {"termite_spray_required": False}}},
                    ],
                },
                {
                    "key": "steel_type",
                    "options": [
                        {"value": "local_60g", "impact": {"set_param": {"saria_type": "Local 60G"}}},
                        {"value": "branded", "impact": {"set_param": {"saria_type": "Branded"}}},
                    ],
                },
            ],
        },
        {
            "step_key": "finish",
            "order": 3,
            "visibility": {"when": "structure_type", "eq": "finished"},
            "questions": [
                {
                    "key": "floor_type",
                    "options": [
                        {"value": "marble", "impact": {"set_param": {"flooring_type": "Marble"}}},
                        {"value": "tile", "impact": {"set_param": {"flooring_type": "Tile"}}},
                    ],
                },
                {
                    "key": "ceiling",
                    "options": [
                        {"value": "yes", "impact": {"set_param": {"ceiling_type": True}}},
                        {"value": "no", "impact": {"set_param": {"ceiling_type": False}}},
                    ],
                },
            ],
        },
    ]
}


def _mock_settings(**overrides):
    """Return a MagicMock that behaves like a Construction Setting singleton."""
    defaults = {
        "foundation_rate": 100, "foundation_3f_qty": 1.5, "foundation_4f_qty": 2.0,
        "termite_spray_qty": 1.0, "termite_spray_rate": 5,
        "rori_qty": 0.5, "rori_rate": 10,
        "rait_ravi_qty": 0.3, "rait_ravi_rate": 8,
        "rait_chanab_qty": 0.2, "rait_chanab_rate": 7,
        "ent_qty": 3.0, "ent_awal_plus_rate": 20, "ent_awal_rate": 15, "ent_dom_rate": 10,
        "bajri_qty": 0.4, "bajri_rate": 12,
        "cement_qty": 0.6, "cement_rate": 50,
        "saria_qty": 1.0, "saria_local_rate": 200, "saria_branded_rate": 280,
        "pump_bore_qty": 1, "pump_bore_rate": 50000,
        "bijli_local_rate": 30, "bijli_branded_rate": 50,
        "sanitary_local_rate": 20, "sanitary_branded_rate": 40,
        "gate_16g_rate": 60, "gate_18g_rate": 45,
        "labour_rate": 350,
        # finish fields
        "floor_qty": 1.0, "marble_rate": 150, "tile_rate": 80,
        "floor_labour_rate": 25,
        "bathroom_tile_qty": 0.3, "bathroom_tile_rate": 90,
        "chat_tile_qty": 0.1, "chat_tile_rate": 70,
        "lightning_switchboard_rate": 15,
        "electrical_wiring_local_rate": 35, "electrical_wiring_branded_rate": 55,
        "roof_ceiling_qty": 0.8, "roof_ceiling_rate": 40,
        "wood_martial_ratelocal": 100, "wood_martial_rate": 180, "wood_martial_lab": 50,
        "simple_paint_with_lab_rate": 60, "paint_with_lab_rate": 90,
        "aluminium": 3000, "steel": 2000, "window_qty": 10,
        "sanitary_fitting_local_rate": 25, "sanitary_fitting_branded_rate": 50,
        "sanitary_fitting_lab_rate": 15,
        "steel_stainless_qty": 0.2, "steel_stainless_rate": 500,
        "stair_and_kitchen_marble_rate": 120,
    }
    defaults.update(overrides)
    s = MagicMock()
    for k, v in defaults.items():
        setattr(s, k, v)
    return s


# ===================================================================
# Test: merge_impacts
# ===================================================================

class TestMergeImpacts(unittest.TestCase):
    """Verify deterministic impact merging from questionnaire + answers."""

    def test_empty_answers(self):
        result = merge_impacts(SAMPLE_QUESTIONNAIRE, {})
        self.assertEqual(result["params"], {})
        self.assertEqual(result["enabled_phases"], {"gray"})

    def test_single_answer(self):
        result = merge_impacts(SAMPLE_QUESTIONNAIRE, {"brick_type": "awal"})
        self.assertEqual(result["params"]["ent_type"], "Awal")

    def test_multiple_answers_merged(self):
        answers = {
            "structure_type": "finished",
            "drawing_required": "no",
            "brick_type": "awal_plus",
            "foundation_depth": "4ft",
        }
        result = merge_impacts(SAMPLE_QUESTIONNAIRE, answers)
        self.assertEqual(result["params"]["construction_type"], "both")
        self.assertFalse(result["params"]["drawingRequired"])
        self.assertEqual(result["params"]["ent_type"], "Awal+")
        self.assertEqual(result["params"]["foundation_type"], "4 ft")
        self.assertIn("finish", result["enabled_phases"])

    def test_gray_only_no_finish_phase(self):
        answers = {"structure_type": "gray"}
        result = merge_impacts(SAMPLE_QUESTIONNAIRE, answers)
        self.assertNotIn("finish", result["enabled_phases"])

    def test_unknown_answer_ignored(self):
        answers = {"brick_type": "unknown_value"}
        result = merge_impacts(SAMPLE_QUESTIONNAIRE, answers)
        self.assertNotIn("ent_type", result["params"])

    def test_deterministic_order(self):
        """Later steps overwrite earlier ones for the same param key."""
        q = {
            "steps": [
                {"order": 1, "step_key": "a", "questions": [
                    {"key": "q1", "options": [{"value": "x", "impact": {"set_param": {"p": "from_step1"}}}]}
                ]},
                {"order": 2, "step_key": "b", "questions": [
                    {"key": "q2", "options": [{"value": "y", "impact": {"set_param": {"p": "from_step2"}}}]}
                ]},
            ]
        }
        result = merge_impacts(q, {"q1": "x", "q2": "y"})
        self.assertEqual(result["params"]["p"], "from_step2")

    def test_enable_phase(self):
        answers = {"structure_type": "finished"}
        result = merge_impacts(SAMPLE_QUESTIONNAIRE, answers)
        self.assertIn("finish", result["enabled_phases"])


# ===================================================================
# Test: slab look-up
# ===================================================================

class TestSlabLookup(unittest.TestCase):

    def test_below_first_threshold(self):
        self.assertEqual(_slab(400, KAASOO_SLABS, KAASOO_DEFAULT), 30000)

    def test_exact_threshold_goes_to_next(self):
        # value == 500 is NOT < 500, so it goes past the first slab
        self.assertEqual(_slab(500, KAASOO_SLABS, KAASOO_DEFAULT), 35000)

    def test_above_all_thresholds(self):
        self.assertEqual(_slab(99999, KAASOO_SLABS, KAASOO_DEFAULT), KAASOO_DEFAULT)

    def test_drawing_slabs(self):
        self.assertEqual(_slab(800, DRAWING_SLABS, DRAWING_DEFAULT), 50000)
        self.assertEqual(_slab(1500, DRAWING_SLABS, DRAWING_DEFAULT), 100000)
        self.assertEqual(_slab(6000, DRAWING_SLABS, DRAWING_DEFAULT), DRAWING_DEFAULT)


# ===================================================================
# Test: _item helper
# ===================================================================

class TestItemHelper(unittest.TestCase):

    def test_rounding(self):
        it = _item("Test", "test", 1.23456, "SF", 10.999, 13.57899)
        self.assertEqual(it["qty"], 1.235)
        self.assertEqual(it["rate"], 11.0)
        self.assertEqual(it["cost"], 13.58)


# ===================================================================
# Test: gray structure calculation
# ===================================================================

class TestGrayCalculation(unittest.TestCase):

    def test_foundation_3ft(self):
        s = _mock_settings()
        items = _calc_gray(s, 1000, 1000, "3 ft", False, False,
                           "Dom", "Branded", False, "Branded", "Branded", "16 Gauge")
        foundation = next(i for i in items if i["material_key"] == "foundation")
        # qty = int(1.5 * 1000) = 1500, rate = 100, cost = 150000
        self.assertEqual(foundation["qty"], 1500)
        self.assertEqual(foundation["cost"], 150000)

    def test_foundation_4ft(self):
        s = _mock_settings()
        items = _calc_gray(s, 1000, 1000, "4 ft", False, False,
                           "Dom", "Branded", False, "Branded", "Branded", "16 Gauge")
        foundation = next(i for i in items if i["material_key"] == "foundation")
        # qty = int(2.0 * 1000) = 2000, cost = 200000
        self.assertEqual(foundation["qty"], 2000)
        self.assertEqual(foundation["cost"], 200000)

    def test_drawing_included_when_required(self):
        s = _mock_settings()
        items = _calc_gray(s, 1000, 1200, None, True, False,
                           "Dom", "Branded", False, "Branded", "Branded", "16 Gauge")
        drawing = [i for i in items if i["material_key"] == "drawing"]
        self.assertEqual(len(drawing), 1)
        # 1200 sqft → slab: < 1500 → 75000
        self.assertEqual(drawing[0]["cost"], 75000)

    def test_drawing_excluded_when_not_required(self):
        s = _mock_settings()
        items = _calc_gray(s, 1000, 1200, None, False, False,
                           "Dom", "Branded", False, "Branded", "Branded", "16 Gauge")
        drawing = [i for i in items if i["material_key"] == "drawing"]
        self.assertEqual(len(drawing), 0)

    def test_termite_included_when_required(self):
        s = _mock_settings()
        items = _calc_gray(s, 1000, 1000, None, False, True,
                           "Dom", "Branded", False, "Branded", "Branded", "16 Gauge")
        termite = [i for i in items if i["material_key"] == "termite"]
        self.assertEqual(len(termite), 1)
        # qty = int(1000 * 1.0) = 1000, rate = 5, cost = 5000
        self.assertEqual(termite[0]["cost"], 5000)

    def test_brick_type_awal_plus(self):
        s = _mock_settings()
        items = _calc_gray(s, 1000, 1000, None, False, False,
                           "Awal+", "Branded", False, "Branded", "Branded", "16 Gauge")
        ent = next(i for i in items if i["material_key"] == "ent")
        # qty = int(1000 * 3.0) = 3000, rate = 20
        self.assertEqual(ent["rate"], 20)
        self.assertEqual(ent["cost"], 60000)

    def test_saria_local(self):
        s = _mock_settings()
        items = _calc_gray(s, 1000, 1000, None, False, False,
                           "Dom", "Local 60G", False, "Branded", "Branded", "16 Gauge")
        saria = next(i for i in items if i["material_key"] == "saria")
        self.assertEqual(saria["rate"], 200)

    def test_pump_bore_conditional(self):
        s = _mock_settings()
        items = _calc_gray(s, 1000, 1000, None, False, False,
                           "Dom", "Branded", True, "Branded", "Branded", "16 Gauge")
        bore = [i for i in items if i["material_key"] == "pump_bore"]
        self.assertEqual(len(bore), 1)
        self.assertEqual(bore[0]["cost"], 50000)

    def test_other_expenses_multiplied_by_1_5(self):
        s = _mock_settings()
        items = _calc_gray(s, 1000, 800, None, False, False,
                           "Dom", "Branded", False, "Branded", "Branded", "16 Gauge")
        other = next(i for i in items if i["material_key"] == "other_expenses")
        # covered_area=800 → slab < 900 → 50000 × 1.5 = 75000
        self.assertEqual(other["cost"], 75000)

    def test_total_gray_deterministic(self):
        """All items sum must equal the sum of individual costs."""
        s = _mock_settings()
        items = _calc_gray(s, 1000, 1000, "3 ft", True, True,
                           "Awal", "Branded", True, "Local", "Local", "18 Gauge")
        total = sum(i["cost"] for i in items)
        self.assertGreater(total, 0)
        # Verify each item has required keys
        for item in items:
            for k in ("material", "material_key", "phase", "qty", "unit", "rate", "cost"):
                self.assertIn(k, item)


# ===================================================================
# Test: finish calculation
# ===================================================================

class TestFinishCalculation(unittest.TestCase):

    def test_marble_floor(self):
        s = _mock_settings()
        items = _calc_finish(s, 1000, "Marble", "Branded", "Aluminium",
                             "YES", "Branded", "Branded", True)
        floor = next(i for i in items if i["material_key"] == "floor")
        # qty = int(1000*1.0) = 1000, rate = 150, cost = 150000
        self.assertEqual(floor["cost"], 150000)

    def test_tile_floor(self):
        s = _mock_settings()
        items = _calc_finish(s, 1000, "Tile", "Branded", "Aluminium",
                             "YES", "Branded", "Branded", False)
        floor = next(i for i in items if i["material_key"] == "floor")
        self.assertGreater(floor["cost"], 0)

    def test_ceiling_conditional(self):
        s = _mock_settings()
        items_with = _calc_finish(s, 1000, "Marble", "Branded", "Aluminium",
                                  "YES", "Branded", "Branded", True)
        items_without = _calc_finish(s, 1000, "Marble", "Branded", "Aluminium",
                                     "YES", "Branded", "Branded", False)
        ceil_with = [i for i in items_with if i["material_key"] == "roof_ceiling"]
        ceil_without = [i for i in items_without if i["material_key"] == "roof_ceiling"]
        self.assertEqual(len(ceil_with), 1)
        self.assertEqual(len(ceil_without), 0)

    def test_all_finish_items_have_finish_phase(self):
        s = _mock_settings()
        items = _calc_finish(s, 1000, "Marble", "Branded", "Aluminium",
                             "YES", "Branded", "Branded", True)
        for item in items:
            self.assertEqual(item["phase"], "finish")

    def test_paint_simple_vs_filling(self):
        s = _mock_settings()
        items_yes = _calc_finish(s, 1000, "Marble", "Branded", "Aluminium",
                                 "YES", "Branded", "Branded", False)
        items_no = _calc_finish(s, 1000, "Marble", "Branded", "Aluminium",
                                "NO", "Branded", "Branded", False)
        paint_yes = next(i for i in items_yes if i["material_key"] == "paint")
        paint_no = next(i for i in items_no if i["material_key"] == "paint")
        # YES → 90 * 1000 = 90000, NO → 60 * 1000 = 60000
        self.assertEqual(paint_yes["cost"], 90000)
        self.assertEqual(paint_no["cost"], 60000)


# ===================================================================
# Test: full compute (mocked frappe.get_single)
# ===================================================================

class TestPKCompute(unittest.TestCase):

    @patch("construction.estimate_engine.pk_calculator.frappe")
    def test_gray_only(self, mock_frappe):
        mock_frappe.get_single.return_value = _mock_settings()
        from construction.estimate_engine.pk_calculator import compute
        result = compute("Lahore", 1000, 1000, {"construction_type": "gray"})
        self.assertIn("line_items", result)
        self.assertIn("totals", result)
        self.assertGreater(result["totals"]["overall"], 0)
        self.assertNotIn("phase_breakdown", result)

    @patch("construction.estimate_engine.pk_calculator.frappe")
    def test_gray_plus_finish(self, mock_frappe):
        mock_frappe.get_single.return_value = _mock_settings()
        from construction.estimate_engine.pk_calculator import compute
        result = compute("Lahore", 1000, 1000, {"construction_type": "both"})
        self.assertIn("phase_breakdown", result)
        self.assertIn("gray", result["phase_breakdown"])
        self.assertIn("finish", result["phase_breakdown"])
        self.assertEqual(
            result["totals"]["overall"],
            result["totals"]["gray"] + result["totals"]["finish"],
        )


if __name__ == "__main__":
    unittest.main()
