"""
Pakistan house-estimate calculator.

Preserves the exact calculation logic from the original
``Construction Setting`` endpoint, refactored into a line-items format.

Floor-aware items (foundation, termite spray, sub-base materials) use the
ground-floor area when ``floor_areas`` is provided so multi-storey
estimates do not double-count single-pour work.
"""

import frappe


# ---------------------------------------------------------------------------
# Slab tables (unchanged from original code)
# ---------------------------------------------------------------------------

DRAWING_SLABS = [
    (1000, 50000), (1500, 75000), (2000, 100000), (2500, 125000),
    (3000, 150000), (3500, 175000), (5000, 200000),
]
DRAWING_DEFAULT = 250000

KAASOO_SLABS = [
    (500, 30000), (600, 35000), (700, 40000), (800, 45000),
    (900, 50000), (1000, 55000), (1200, 80000), (1500, 100000),
    (1800, 120000), (2250, 150000), (2500, 180000), (2800, 200000),
    (3200, 250000), (3500, 280000), (4000, 300000),
]
KAASOO_DEFAULT = 350000

# "Other expenses" uses the same thresholds as kaasoo but on covered_area
OTHER_SLABS = [
    (500, 30000), (600, 35000), (700, 40000), (800, 45000),
    (900, 50000), (1000, 55000), (1200, 80000), (1500, 100000),
    (1800, 120000), (2250, 150000), (2500, 180000), (2800, 200000),
    (3200, 250000), (3500, 280000), (4000, 300000),
]
OTHER_DEFAULT = 350000


def _slab(value, slabs, default):
    """Look up a slab-based cost."""
    for threshold, cost in slabs:
        if value < threshold:
            return cost
    return default


_NO_DISPLAY_KEYS = frozenset({
    "foundation", "drawing", "kaasoo", "pump_bore", "bijli",
    "sanitary_pipes", "other_expenses", "switchboard", "wiring",
    "wood_martial", "window", "sanitary_fitting", "steel_grill",
})


def _item(material, key, qty, unit, rate, cost, phase="gray"):
    return {
        "material": material,
        "material_key": key,
        "phase": phase,
        "qty": round(float(qty), 3),
        "unit": unit,
        "rate": round(float(rate), 2),
        "cost": round(float(cost), 2),
        "display": key not in _NO_DISPLAY_KEYS,
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def compute(city, plot_size_sqft, covered_area_sqft, params, floor_areas=None):
    """
    Compute a Pakistan house estimate.

    Parameters
    ----------
    city : str              -- (currently unused; rates come from Construction Setting)
    plot_size_sqft : float
    covered_area_sqft : float -- sum of per-floor covered areas
    params : dict           -- merged parameter map from questionnaire impacts
    floor_areas : list[float] | None
                            -- per-floor covered areas in sq ft (index 0 = ground).
                               When omitted, treated as a single floor of
                               ``covered_area_sqft``.
    """
    settings = frappe.get_single("Construction Setting")
    ps = plot_size_sqft
    ca = covered_area_sqft
    # Ground-floor area drives single-pour items (foundation, sub-base).
    if floor_areas:
        ground = float(floor_areas[0])
    else:
        ground = ca

    # ---- extract params with safe defaults ----
    construction_type = params.get("construction_type", "gray")
    foundation_type = params.get("foundation_type")
    drawing_required = params.get("drawingRequired", False)
    termite_spray_required = params.get("termite_spray_required", False)
    ent_type = params.get("ent_type", "Dom")
    saria_type = params.get("saria_type", "Branded")
    pump_bore_required = params.get("pump_bore_required", False)
    pipe_type = params.get("pipe_type", "Branded")
    sanitary_type = params.get("sanitary_type", "Branded")
    gauge = params.get("gauge", "16 Gauge")

    flooring_type = params.get("flooring_type", "Marble")
    wiring_type = params.get("wiring_type", "Branded")
    window_type = params.get("window_type", "Aluminium")
    paint_type = params.get("paint_type", "YES")
    wood_type = params.get("wood_type", "Branded")
    sanitaryfiting_type = params.get("sanitaryfiting_type", "Branded")
    ceiling_type = params.get("ceiling_type", False)

    # ---- gray structure line items ----
    gray_items = _calc_gray(
        settings, ps, ca, foundation_type, drawing_required,
        termite_spray_required, ent_type, saria_type,
        pump_bore_required, pipe_type, sanitary_type, gauge,
        ground=ground,
    )

    # ---- finish line items ----
    finish_items = []
    if construction_type in ("finish", "both"):
        finish_items = _calc_finish(
            settings, ca, flooring_type, wiring_type, window_type,
            paint_type, wood_type, sanitaryfiting_type, ceiling_type,
        )

    gray_total = sum(i["cost"] for i in gray_items)
    finish_total = sum(i["cost"] for i in finish_items)
    overall = gray_total + finish_total

    result = {
        "line_items": gray_items + finish_items,
        "totals": {
            "gray": round(gray_total, 2),
            "overall": round(overall, 2),
        },
    }

    if construction_type in ("finish", "both"):
        result["totals"]["finish"] = round(finish_total, 2)
        result["phase_breakdown"] = {
            "gray": {"line_items": gray_items, "total": round(gray_total, 2)},
            "finish": {"line_items": finish_items, "total": round(finish_total, 2)},
        }

    return result


# ---------------------------------------------------------------------------
# Gray structure
# ---------------------------------------------------------------------------

def _calc_gray(settings, ps, ca, foundation_type, drawing_required,
               termite_spray_required, ent_type, saria_type,
               pump_bore_required, pipe_type, sanitary_type, gauge,
               ground=None):
    """``ground`` is the ground-floor covered area for single-pour items;
    falls back to total ``ca`` so existing single-floor callers keep their
    semantics."""
    if ground is None:
        ground = ca
    items = []

    # 1. Foundation -- single pour, scales with plot size only
    f_rate = settings.foundation_rate or 0
    if foundation_type == "3 ft":
        f_qty = int((settings.foundation_3f_qty or 0) * ps)
    elif foundation_type == "4 ft":
        f_qty = int((settings.foundation_4f_qty or 0) * ps)
    else:
        f_qty = 0
    items.append(_item("Foundation", "foundation", f_qty, "SF", f_rate, f_qty * f_rate))

    # 2. Drawing (conditional, slab-based on total covered area)
    if drawing_required:
        dc = _slab(ca, DRAWING_SLABS, DRAWING_DEFAULT)
        items.append(_item("Drawing", "drawing", 1, "Lot", dc, dc))

    # 3. Termite Spray (conditional) -- ground level only
    if termite_spray_required:
        t_qty = int(ground * (settings.termite_spray_qty or 0))
        t_rate = settings.termite_spray_rate or 0
        items.append(_item("Termite Spray", "termite", t_qty, "SF", t_rate, t_qty * t_rate))

    # 4. Rori -- sub-base, ground only
    rori_qty = int(ground * (settings.rori_qty or 0))
    rori_rate = settings.rori_rate or 0
    items.append(_item("Rori", "rori", rori_qty, "SF", rori_rate, rori_qty * rori_rate))

    # 5. Rait Ravi -- sub-base, ground only
    rr_qty = int(ground * (settings.rait_ravi_qty or 0))
    rr_rate = settings.rait_ravi_rate or 0
    items.append(_item("Rait Ravi", "rait_ravi", rr_qty, "SF", rr_rate, rr_qty * rr_rate))

    # 6. Rait Chanab -- sub-base, ground only
    rc_qty = int(ground * (settings.rait_chanab_qty or 0))
    rc_rate = settings.rait_chanab_rate or 0
    items.append(_item("Rait Chanab", "rait_chanab", rc_qty, "SF", rc_rate, rc_qty * rc_rate))

    # 7. Ent (Bricks)
    ent_qty = int(ca * (settings.ent_qty or 0))
    if ent_type == "Awal+":
        ent_rate = settings.ent_awal_plus_rate or 0
    elif ent_type == "Awal":
        ent_rate = settings.ent_awal_rate or 0
    else:
        ent_rate = settings.ent_dom_rate or 0
    items.append(_item("Ent (Bricks)", "ent", ent_qty, "Pcs", ent_rate, ent_qty * ent_rate))

    # 8. Kaasoo Bahari (slab-based on plot_size)
    kaasoo_cost = _slab(ps, KAASOO_SLABS, KAASOO_DEFAULT)
    items.append(_item("Kaasoo Bahari", "kaasoo", 1, "Lot", kaasoo_cost, kaasoo_cost))

    # 9. Bajri
    b_qty = int(ca * (settings.bajri_qty or 0))
    b_rate = settings.bajri_rate or 0
    items.append(_item("Bajri", "bajri", b_qty, "SF", b_rate, b_qty * b_rate))

    # 10. Cement
    c_qty = int(ca * (settings.cement_qty or 0))
    c_rate = settings.cement_rate or 0
    items.append(_item("Cement", "cement", c_qty, "Bags", c_rate, c_qty * c_rate))

    # 11. Saria (Steel)
    s_qty = int(ca * (settings.saria_qty or 0))
    if saria_type == "Local 60G":
        s_rate = settings.saria_local_rate or 0
    else:
        s_rate = settings.saria_branded_rate or 0
    items.append(_item("Saria (Steel)", "saria", s_qty, "KGs", s_rate, s_qty * s_rate))

    # 12. Pump Bore (conditional)
    if pump_bore_required:
        pb_qty = int(settings.pump_bore_qty or 0)
        pb_rate = settings.pump_bore_rate or 0
        items.append(_item("Pump Bore", "pump_bore", pb_qty, "Lot", pb_rate, pb_qty * pb_rate))

    # 13. Bijli (Electric) Pipes
    if pipe_type == "Local":
        bj_rate = settings.bijli_local_rate or 0
    else:
        bj_rate = settings.bijli_branded_rate or 0
    items.append(_item("Bijli Pipes", "bijli", ca, "SF", bj_rate, ca * bj_rate))

    # 14. Sanitary Pipes
    if sanitary_type == "Local":
        san_rate = settings.sanitary_local_rate or 0
    else:
        san_rate = settings.sanitary_branded_rate or 0
    items.append(_item("Sanitary Pipes", "sanitary_pipes", ca, "SF", san_rate, ca * san_rate))

    # 15. Gate & Steel Chaukhat
    if gauge == "16 Gauge":
        g_rate = settings.gate_16g_rate or 0
    else:
        g_rate = settings.gate_18g_rate or 0
    items.append(_item("Gate & Steel Chaukhat", "gate_chaukhat", ca, "SF", g_rate, ca * g_rate))

    # 16. Labour
    l_qty = int(ca)
    l_rate = settings.labour_rate or 0
    items.append(_item("Labour", "labour", l_qty, "SF", l_rate, l_qty * l_rate))

    # 17. Other Expenses (slab on covered_area * 1.5)
    other_base = _slab(ca, OTHER_SLABS, OTHER_DEFAULT)
    other_cost = int(other_base * 1.5)
    items.append(_item("Other Expenses", "other_expenses", 1, "Lot", other_cost, other_cost))

    return items


# ---------------------------------------------------------------------------
# Finish
# ---------------------------------------------------------------------------

def _calc_finish(settings, ca, flooring_type, wiring_type, window_type,
                 paint_type, wood_type, sanitaryfiting_type, ceiling_type):
    items = []

    # 1. Floor
    floor_qty = int(ca * (settings.floor_qty or 0))
    if flooring_type == "Marble":
        fl_rate = settings.marble_rate or 0
        fl_qty_m = round(floor_qty * 0.092903, 2)  # sqft -> sqm
        fl_cost = fl_qty_m * fl_rate
        items.append(_item("Floor (Marble)", "floor", fl_qty_m, "m", fl_rate, fl_cost, "finish"))
    else:
        fl_rate = settings.tile_rate or 0
        fl_qty_m = round(floor_qty * 0.092903, 2)  # sqft -> sqm
        fl_cost = fl_qty_m * fl_rate
        items.append(_item("Floor (Tile)", "floor", fl_qty_m, "m", fl_rate, fl_cost, "finish"))

    # 2. Flooring Labour
    fl_lab_rate = settings.floor_labour_rate or 0
    items.append(_item("Flooring Labour", "floor_labour", floor_qty, "SF",
                       fl_lab_rate, floor_qty * fl_lab_rate, "finish"))

    # 3. Bathroom Tile
    bt_qty = int(ca * (settings.bathroom_tile_qty or 0))
    bt_rate = settings.bathroom_tile_rate or 0
    items.append(_item("Bathroom Tile", "bathroom_tile", bt_qty, "m",
                       bt_rate, bt_qty * bt_rate, "finish"))

    # 4. Chat Tile
    ct_qty = int(ca * (settings.chat_tile_qty or 0))
    ct_rate = settings.chat_tile_rate or 0
    items.append(_item("Chat Tile", "chat_tile", ct_qty, "Pc",
                       ct_rate, ct_qty * ct_rate, "finish"))

    # 5. Lightning Switchboard
    sb_rate = settings.lightning_switchboard_rate or 0
    items.append(_item("Lightning Switchboard", "switchboard", ca, "SF",
                       sb_rate, ca * sb_rate, "finish"))

    # 6. Electrical Wiring
    if wiring_type == "Local":
        ew_rate = settings.electrical_wiring_local_rate or 0
    else:
        ew_rate = settings.electrical_wiring_branded_rate or 0
    items.append(_item("Electrical Wiring", "wiring", ca, "SF",
                       ew_rate, ca * ew_rate, "finish"))

    # 7. Roof Ceiling (conditional)
    if ceiling_type:
        ceil_qty = int(ca * (settings.roof_ceiling_qty or 0))
        ceil_rate = settings.roof_ceiling_rate or 0
        items.append(_item("Roof Ceiling", "roof_ceiling", ceil_qty, "SF",
                           ceil_rate, ceil_qty * ceil_rate, "finish"))

    # 8. Wood Martial
    if wood_type == "Local":
        w_rate = settings.wood_martial_ratelocal or 0
    else:
        w_rate = settings.wood_martial_rate or 0
    w_qty = int(ca)
    w_cost = w_qty * w_rate
    w_lab = w_qty * (settings.wood_martial_lab or 0)
    items.append(_item("Wood Martial", "wood_martial", w_qty, "SF",
                       w_rate, w_cost + w_lab, "finish"))

    # 9. Paint
    if paint_type == "NO":
        p_rate = settings.simple_paint_with_lab_rate or 0
    else:
        p_rate = settings.paint_with_lab_rate or 0
    items.append(_item("Paint", "paint", ca, "SF", p_rate, ca * p_rate, "finish"))

    # 10. Windows
    if window_type == "Aluminium":
        win_rate = settings.aluminium or 0
    else:
        win_rate = settings.steel or 0
    win_qty = int(settings.window_qty or 0)
    items.append(_item("Windows", "window", win_qty, "Pcs",
                       win_rate, win_qty * win_rate, "finish"))

    # 11. Sanitary Fitting
    if sanitaryfiting_type == "Local":
        sf_rate = settings.sanitary_fitting_local_rate or 0
    else:
        sf_rate = settings.sanitary_fitting_branded_rate or 0
    sf_qty = int(ca)
    sf_cost = sf_qty * sf_rate
    sf_lab = sf_qty * (settings.sanitary_fitting_lab_rate or 0)
    items.append(_item("Sanitary Fitting", "sanitary_fitting", sf_qty, "SF",
                       sf_rate, sf_cost + sf_lab, "finish"))

    # 12. Steel Stainless Grill
    gr_qty = int(ca * (settings.steel_stainless_qty or 0))
    gr_rate = settings.steel_stainless_rate or 0
    items.append(_item("Steel Stainless Grill", "steel_grill", gr_qty, "SF",
                       gr_rate, gr_qty * gr_rate, "finish"))

    # 13. Stair & Kitchen Marble
    skm_rate = settings.stair_and_kitchen_marble_rate or 0
    items.append(_item("Stair & Kitchen Marble", "stair_kitchen_marble", ca, "SF",
                       skm_rate, ca * skm_rate, "finish"))

    return items
