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

from construction.estimate_engine.engine import _extract_basement, merge_impacts
from construction.estimate_engine.pk_calculator import (
    DRAWING_DEFAULT,
    DRAWING_SLABS,
    KAASOO_DEFAULT,
    KAASOO_SLABS,
    OTHER_DEFAULT,
    OTHER_SLABS,
    _calc_basement,
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
        # basement works
        "basement_excavation_qty": 10.0, "basement_excavation_rate": 30,
        "basement_wall_height_ft": 10.0, "basement_retaining_rate": 450,
        "basement_waterproofing_rate": 120,
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
        # qty = int(1000*1.0) = 1000 sqft, rate = 150/sqft, cost = 150000.
        # This assertion is ORIGINAL and was correct: it started failing when a
        # refactor re-expressed the qty in sqm without scaling the per-sqft rate
        # up by 10.7639, quietly cutting flooring cost to a tenth. Do not
        # "update" it to match the code -- it is the canary.
        self.assertEqual(floor["unit"], "SF")
        self.assertEqual(floor["qty"], 1000)
        self.assertEqual(floor["cost"], 150000)

    def test_tile_floor(self):
        s = _mock_settings()
        items = _calc_finish(s, 1000, "Tile", "Branded", "Aluminium",
                             "YES", "Branded", "Branded", False)
        floor = next(i for i in items if i["material_key"] == "floor")
        # Tile is priced on the same per-sqft basis as marble: 1000 sqft @ 80.
        self.assertEqual(floor["unit"], "SF")
        self.assertEqual(floor["cost"], 80000)

    def test_floor_priced_per_sqft_not_sqm(self):
        # Regression guard for the 10.76x flooring undercharge: pricing the sqm
        # qty at the per-sqft rate would give 92.9 * 150 = 13,935.
        s = _mock_settings()
        items = _calc_finish(s, 1000, "Marble", "Branded", "Aluminium",
                             "YES", "Branded", "Branded", False)
        floor = next(i for i in items if i["material_key"] == "floor")
        self.assertNotAlmostEqual(floor["cost"], 13935.0, places=0)
        self.assertAlmostEqual(floor["cost"], floor["qty"] * floor["rate"], places=2)

    def test_bathroom_tile_is_per_sqft(self):
        # Owner-confirmed: bathroom_tile_rate is per SQ FT and the qty is a
        # sqft area, so cost = qty * rate. The line used to report unit "m",
        # claiming square metres for a quantity that was never converted --
        # cosmetic, but it made a correct number look like a 10.76x error.
        s = _mock_settings()
        items = _calc_finish(s, 1000, "Marble", "Branded", "Aluminium",
                             "YES", "Branded", "Branded", False)
        bt = next(i for i in items if i["material_key"] == "bathroom_tile")
        self.assertEqual(bt["unit"], "SF")
        # qty = int(1000 * 0.3) = 300 sqft @ 90/sqft
        self.assertEqual(bt["qty"], 300)
        self.assertEqual(bt["cost"], 27000)

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


# ===================================================================
# Test: basement works
#
# A basement used to cost exactly what an above-grade storey cost -- the client
# folded its area into covered_area_sqft and no below-grade work was priced at
# all. "basement" never even reached the calculator: it is collected on the
# plot-info step, so it is not a questionnaire answer and never became a param.
# ===================================================================

class TestBasementExtraction(unittest.TestCase):
    """The engine has to read basement off `answers` directly."""

    def test_absent_when_flag_false(self):
        b = _extract_basement({"basement": False, "basement_area_sqft": 900})
        self.assertFalse(b["enabled"])

    def test_absent_when_no_answers(self):
        self.assertFalse(_extract_basement({})["enabled"])
        self.assertFalse(_extract_basement(None)["enabled"])

    def test_enabled_with_area(self):
        b = _extract_basement({"basement": True, "basement_area_sqft": 900})
        self.assertTrue(b["enabled"])
        self.assertEqual(b["area_sqft"], 900.0)

    def test_flag_without_area_degrades_to_off(self):
        # An older client sends the flag but no area. Better to price no
        # basement than to emit zero-qty basement lines.
        b = _extract_basement({"basement": True})
        self.assertFalse(b["enabled"])

    def test_garbage_area_degrades_to_off(self):
        for bad in ("abc", None, -5, 0, float("inf"), float("nan")):
            b = _extract_basement({"basement": True, "basement_area_sqft": bad})
            self.assertFalse(b["enabled"], msg="area=%r should disable" % (bad,))


class TestBasementCalculation(unittest.TestCase):

    def test_no_items_for_zero_area(self):
        self.assertEqual(_calc_basement(_mock_settings(), 0), [])

    def test_excavation_is_area_times_dig_depth(self):
        items = _calc_basement(_mock_settings(), 900)
        exc = next(i for i in items if i["material_key"] == "basement_excavation")
        # 900 sqft x 10 cft/sqft dug = 9,000 cft @ 30
        self.assertEqual(exc["qty"], 9000.0)
        self.assertEqual(exc["unit"], "CFT")
        self.assertEqual(exc["cost"], 270000.0)

    def test_retaining_wall_uses_square_perimeter(self):
        items = _calc_basement(_mock_settings(), 900)
        ret = next(i for i in items if i["material_key"] == "basement_retaining")
        # perimeter of an equivalent square = 4 * sqrt(900) = 120 ft,
        # x 10 ft high = 1,200 sqft of wall face @ 450
        self.assertEqual(ret["qty"], 1200.0)
        self.assertEqual(ret["cost"], 540000.0)

    def test_waterproofing_covers_slab_plus_walls(self):
        items = _calc_basement(_mock_settings(), 900)
        wp = next(i for i in items if i["material_key"] == "basement_waterproofing")
        # 900 slab + 1,200 wall = 2,100 sqft @ 120
        self.assertEqual(wp["qty"], 2100.0)
        self.assertEqual(wp["cost"], 252000.0)

    def test_zero_rate_drops_the_line(self):
        # Rates ship as placeholders; an owner who zeroes one should not get a
        # free line item priced at nothing.
        items = _calc_basement(_mock_settings(basement_retaining_rate=0), 900)
        keys = {i["material_key"] for i in items}
        self.assertNotIn("basement_retaining", keys)
        self.assertIn("basement_excavation", keys)

    def test_all_lines_are_gray_phase_and_displayed(self):
        for it in _calc_basement(_mock_settings(), 900):
            self.assertEqual(it["phase"], "gray")
            self.assertTrue(it["display"])


class TestBasementInCompute(unittest.TestCase):
    """compute() must add the works AND keep the totals reconciling."""

    @patch("construction.estimate_engine.pk_calculator.frappe")
    def test_basement_adds_works_on_top_of_floor_area(self, mock_frappe):
        mock_frappe.get_single.return_value = _mock_settings()
        from construction.estimate_engine.pk_calculator import compute

        # Same covered area both times, so the ONLY difference is the works.
        without = compute("Lahore", 2000, 1800, {"construction_type": "gray"})
        with_b = compute(
            "Lahore", 2000, 1800, {"construction_type": "gray"},
            basement={"enabled": True, "area_sqft": 900},
        )

        works = [i for i in with_b["line_items"]
                 if i["material_key"].startswith("basement")]
        self.assertEqual(len(works), 3)

        delta = with_b["totals"]["overall"] - without["totals"]["overall"]
        self.assertAlmostEqual(delta, sum(i["cost"] for i in works), places=2)

    @patch("construction.estimate_engine.pk_calculator.frappe")
    def test_totals_still_reconcile(self, mock_frappe):
        mock_frappe.get_single.return_value = _mock_settings()
        from construction.estimate_engine.pk_calculator import compute

        r = compute(
            "Lahore", 2000, 1800, {"construction_type": "both"},
            basement={"enabled": True, "area_sqft": 900},
        )
        self.assertAlmostEqual(
            r["totals"]["overall"],
            sum(i["cost"] for i in r["line_items"]),
            places=2,
        )
        self.assertAlmostEqual(
            r["totals"]["overall"],
            r["totals"]["gray"] + r["totals"]["finish"],
            places=2,
        )

    @patch("construction.estimate_engine.pk_calculator.frappe")
    def test_no_basement_means_no_basement_lines(self, mock_frappe):
        mock_frappe.get_single.return_value = _mock_settings()
        from construction.estimate_engine.pk_calculator import compute

        for arg in (None, {"enabled": False, "area_sqft": 0}):
            r = compute("Lahore", 2000, 1800, {"construction_type": "gray"},
                        basement=arg)
            self.assertFalse(
                any(i["material_key"].startswith("basement")
                    for i in r["line_items"])
            )


if __name__ == "__main__":
    unittest.main()
