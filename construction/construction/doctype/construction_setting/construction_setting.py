# Copyright (c) 2025, ali waqar and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class ConstructionSetting(Document):
    pass

# @frappe.whitelist(allow_guest=True)
# def get_estimate(plot_size, covered_area, foundation_type=None, termite_spray_required=False, ent_type=None,
#                  saria_type=None, pump_bore_required=False, pipe_type=None, sanitary_type=None, gauge=None):
#     try:
#         plot_size = float(plot_size)
#         covered_area = float(covered_area)
#         termite_spray_required = str(termite_spray_required).lower() in ['yes', '1', 'true']
#         pump_bore_required = str(pump_bore_required).lower() in ['yes', '1', 'true']

#         settings = frappe.get_single("Construction Setting")
#         # ---------------- Total Cost ----------------
#         total_cost = (
#             drawing_cost + foundation_cost + termite_cost +
#             rori_cost + rait_ravi_cost + rait_chanab_cost +
#             ent_cost + kaasoo_cost + bajri_cost + cement_cost +
#             saria_cost + pump_bore_cost + bijli_cost + sanitary_cost +
#             gate_cost + labour_cost + other_cost
#         )

#         return {
#             "success": True,
#             "drawing": {"cost": round(drawing_cost, 2)},
#             "foundation": {"qty": foundation_qty, "rate": foundation_rate, "cost": round(foundation_cost, 2)},
#             "termite": {"qty": spray_qty, "rate": settings.termite_spray_rate, "cost": round(termite_cost, 2)},
#             "rori": {"qty": rori_qty, "rate": settings.rori_rate, "cost": round(rori_cost, 2)},
#             "rait_ravi": {"qty": rait_ravi_qty, "rate": settings.rait_ravi_rate, "cost": round(rait_ravi_cost, 2)},
#             "rait_chanab": {"qty": rait_chanab_qty, "rate": settings.rait_chanab_rate, "cost": round(rait_chanab_cost, 2)},
#             "ent": {"qty": ent_qty, "rate": ent_rate, "cost": round(ent_cost, 2)},
#             "bajri": {"qty": bajri_qty, "rate": settings.bajri_rate, "cost": round(bajri_cost, 2)},
#             "cement": {"qty": cement_qty, "rate": settings.cement_rate, "cost": round(cement_cost, 2)},
#             "saria": {"qty": saria_qty, "rate": saria_rate, "cost": round(saria_cost, 2)},
#             "pump_bore": {"qty": pump_bore_qty, "rate": pump_bore_rate, "cost": round(pump_bore_cost, 2)},
#             "bijli": { "rate": bijli_rate, "cost": round(bijli_cost, 2)},
#             "sanitary": { "rate": sanitary_rate, "cost": round(sanitary_cost, 2)},
#             "gate_chaukhat": { "rate": gate_rate, "cost": round(gate_cost, 2)},
#             "labour": {"qty": labour_qty, "rate": labour_rate, "cost": round(labour_cost, 2)},
#             "kaasoo": {"cost": round(kaasoo_cost, 2)},
#             "other_expenses": {"qty": other_qty, "rate": other_expenses, "cost": round(other_cost, 2)},
#             "total_cost": round(total_cost, 2)
#         }
#     except Exception as e:

#         return {"success": False, "error": str(e)}

def format_amount(value):
    try:
        return "{:,.0f}".format(value)   # without decimal, with commas
    except:
        return value
    
@frappe.whitelist(allow_guest=True)
def get_estimate(plot_size, covered_area, foundation_type=None, drawingRequired=False,termite_spray_required=False, ent_type=None,
                 saria_type=None, pump_bore_required=False, pipe_type=None, sanitary_type=None, gauge=None,
                 construction_type="gray", flooring_type=None,wiring_type=None, window_type=None, paint_type=None, wood_type=None,
                 ceiling_type=False,sanitaryfiting_type=None):
    try:
        plot_size = float(plot_size)
        covered_area = float(covered_area)

        # normalize bools
        termite_spray_required = str(termite_spray_required).lower() in ['yes', '1', 'true']
        drawingRequired = str(drawingRequired).lower() in ['yes', '1', 'true']
        ceiling_type = str(ceiling_type).lower() in ['yes', '1', 'true']

        pump_bore_required = str(pump_bore_required).lower() in ['yes', '1', 'true']

        settings = frappe.get_single("Construction Setting")

        # ---------------- GRAY ESTIMATE ----------------
        gray_total, gray_details = calculate_gray_estimate(settings, plot_size, covered_area,
                                                           foundation_type, drawingRequired,termite_spray_required,
                                                           ent_type, saria_type, pump_bore_required,
                                                           pipe_type, sanitary_type, gauge)

        # ---------------- FINISH ESTIMATE ----------------
        finish_total, finish_details = 0, {}
        if construction_type in ["finish", "both"]:
            finish_total, finish_details = calculate_finish_estimate(settings, covered_area, flooring_type,wiring_type, window_type, paint_type, wood_type,
                 ceiling_type,sanitaryfiting_type)


        # ---------------- OVERALL ----------------
        overall_total = gray_total + finish_total

        # Base response
        response = {
            "success": True,
            "gray": {
                "details": gray_details,
                "total": round(gray_total, 2)
            },
            "overall_total": round(overall_total, 2)
        }

        # Sirf tab add karo jab construction_type finish ya both ho
        if construction_type in ["finish", "both"]:
            response["finish"] = {
                "details": finish_details,
                "total": round(finish_total, 2)
            }

        return response

    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Construction API Error")
        return {
            "success": False,
            "overall_total": 0.0,
            "error": str(e)
        }


# ---------------- HELPER: Finish Estimate ----------------


def calculate_finish_estimate(settings, covered_area, flooring_type=None,
                              wiring_type=None, window_type=None, paint_type=None, wood_type=None,
                              sanitaryfiting_type=None, ceiling_type=None):
    total = 0
    details = {}

    # Helper function: cost ko integer aur commas ke sath format kare
    def format_cost(value):
        return "{:,}".format(int(round(value)))

    # ---------------- Floor ----------------
    floor_qty = int(covered_area * settings.floor_qty)

    if flooring_type == "Marble":
        rate = settings.marble_rate
        floor_cost = floor_qty * rate
        details["floor"] = {
            "qty": f"{floor_qty}m",
            "rate": rate,
            "cost": format_cost(floor_cost)
        }
    else:  # Tile case
        rate = settings.tile_rate * 10.7639
        qty = floor_qty * 0.092903
        floor_cost = qty * rate
        details["floor"] = {
            "qty": f"{round(qty, 2)}m",
            "rate": int(rate),
            "cost": format_cost(floor_cost)
        }
    total += floor_cost

    # Flooring labour
    floor_labour_cost = floor_qty * settings.floor_labour_rate
    total += floor_labour_cost
    details["floor_labour"] = {
        "qty": f"{floor_qty}sf",
        "rate": settings.floor_labour_rate,
        "cost": format_cost(floor_labour_cost)
    }

    # ---------------- Bathroom Tile ----------------
    bath_tile_qty = int(covered_area * settings.bathroom_tile_qty)
    bath_tile_cost = bath_tile_qty * settings.bathroom_tile_rate
    total += bath_tile_cost
    details["bathroom_tile"] = {
        "qty": f"{bath_tile_qty}m",
        "rate": settings.bathroom_tile_rate,
        "cost": format_cost(bath_tile_cost)
    }

    # ---------------- Chat Tile ----------------
    chat_tile_qty = int(covered_area * settings.chat_tile_qty)
    chat_tile_cost = chat_tile_qty * settings.chat_tile_rate
    total += chat_tile_cost
    details["chat_tile"] = {
        "qty": f"{chat_tile_qty}Pc",
        "rate": settings.chat_tile_rate,
        "cost": format_cost(chat_tile_cost)
    }

    # ---------------- Lightning Switchboard ----------------
    switchboard_cost = covered_area * settings.lightning_switchboard_rate
    total += switchboard_cost
    details["switchboard"] = {"cost": format_cost(switchboard_cost)}

    # ---------------- Electrical Wiring ----------------
    if wiring_type == "Local":
        wiring_rate = settings.electrical_wiring_local_rate
    else:
        wiring_rate = settings.electrical_wiring_branded_rate
    wiring_cost = covered_area * wiring_rate
    total += wiring_cost
    details["wiring"] = {"cost": format_cost(wiring_cost)}

    # ---------------- Roof Ceiling ----------------
    if ceiling_type:
        ceiling_qty = int(covered_area * settings.roof_ceiling_qty)
        ceiling_cost = ceiling_qty * settings.roof_ceiling_rate
        total += ceiling_cost
        details["roof_ceiling"] = {
            "qty": f"{ceiling_qty}sf",
            "rate": settings.roof_ceiling_rate,
            "cost": format_cost(ceiling_cost)
        }

    # ---------------- Wood Martial ----------------
    if wood_type == "Local":
        wood_rate = settings.wood_martial_ratelocal
    else:
        wood_rate = settings.wood_martial_rate
    wood_qty = int(covered_area)
    wood_cost = wood_qty * wood_rate
    wood_labour_cost = wood_qty * settings.wood_martial_lab
    total += wood_cost + wood_labour_cost
    details["wood_martial"] = {
        "cost": format_cost(wood_cost + wood_labour_cost)
    }

    # ---------------- Paint ----------------
    if paint_type == "NO":
        paint_rate = settings.simple_paint_with_lab_rate
    else:
        paint_rate = settings.paint_with_lab_rate
    paint_cost = covered_area * paint_rate
    total += paint_cost
    details["paint"] = {"cost": format_cost(paint_cost)}

    # ---------------- Windows ----------------
    if window_type == "Aluminium":
        win_rate = settings.aluminium
    else:
        win_rate = settings.steel
    win_qty = int(settings.window_qty)
    win_cost = win_qty * win_rate
    total += win_cost
    details["window"] = {"cost": format_cost(win_cost)}

    # ---------------- Sanitary Fitting ----------------
    if sanitaryfiting_type == "Local":
        sanitary_rate = settings.sanitary_fitting_local_rate
    else:
        sanitary_rate = settings.sanitary_fitting_branded_rate
    sanitary_qty = int(covered_area)
    sanitary_cost = sanitary_qty * sanitary_rate
    sanitary_lab_cost = sanitary_qty * settings.sanitary_fitting_lab_rate
    total += sanitary_cost + sanitary_lab_cost
    details["sanitary"] = {
        "cost": format_cost(sanitary_cost + sanitary_lab_cost)
    }

    # ---------------- Steel Stainless Grill ----------------
    grill_qty = int(covered_area * settings.steel_stainless_qty)
    grill_cost = grill_qty * settings.steel_stainless_rate
    total += grill_cost
    details["steel_grill"] = {"cost": format_cost(grill_cost)}

    # ---------------- Stair & Kitchen Marble ----------------
    stair_kitchen_cost = covered_area * settings.stair_and_kitchen_marble_rate
    total += stair_kitchen_cost
    details["stair_kitchen_marble"] = {"cost": format_cost(stair_kitchen_cost)}

    return total, details

def calculate_gray_estimate(
    settings, plot_size, covered_area, foundation_type, drawingRequired,
    termite_spray_required, ent_type, saria_type,
    pump_bore_required, pipe_type, sanitary_type, gauge
):
    # ---------------- Foundation ----------------
    foundation_rate = settings.foundation_rate
    if foundation_type == "3 ft":
        foundation_qty = int(settings.foundation_3f_qty * plot_size)
    elif foundation_type == "4 ft":
        foundation_qty = int(settings.foundation_4f_qty * plot_size)
    else:
        foundation_qty = 0
    foundation_cost = foundation_qty * foundation_rate

    # ---------------- Termite Spray ----------------
    termite_cost, spray_qty = 0, 0
    if termite_spray_required:
        spray_qty = int(plot_size * settings.termite_spray_qty)
        termite_cost = spray_qty * settings.termite_spray_rate

    # ---------------- Rori ----------------
    rori_qty = int(covered_area * settings.rori_qty)
    rori_cost = rori_qty * settings.rori_rate

    # ---------------- Rait Ravi ----------------
    rait_ravi_qty = int(covered_area * settings.rait_ravi_qty)
    rait_ravi_cost = rait_ravi_qty * settings.rait_ravi_rate

    # ---------------- Rait Chanab ----------------
    rait_chanab_qty = int(covered_area * settings.rait_chanab_qty)
    rait_chanab_cost = rait_chanab_qty * settings.rait_chanab_rate

    # ---------------- Ent (Bricks) ----------------
    ent_qty = int(covered_area * settings.ent_qty)
    if ent_type == "Awal+":
        ent_rate = settings.ent_awal_plus_rate
    elif ent_type == "Awal":
        ent_rate = settings.ent_awal_rate
    elif ent_type == "Dom":
        ent_rate = settings.ent_dom_rate
    else:
        ent_rate = settings.ent_dom_rate
    ent_cost = ent_qty * ent_rate

    # ---------------- Drawing Cost Slab ----------------
    drawing_cost = 0
    if drawingRequired:
        if covered_area < 1000:
            drawing_cost = 50000
        elif covered_area < 1500:
            drawing_cost = 75000
        elif covered_area < 2000:
            drawing_cost = 100000
        elif covered_area < 2500:
            drawing_cost = 125000
        elif covered_area < 3000:
            drawing_cost = 150000
        elif covered_area < 3500:
            drawing_cost = 175000
        elif covered_area < 5000:
            drawing_cost = 200000
        else:
            drawing_cost = 250000

    # ---------------- Kaasoo Bahari Slab ----------------
    if plot_size < 500:
        kaasoo_cost = 30000
    elif plot_size < 600:
        kaasoo_cost = 35000
    elif plot_size < 700:
        kaasoo_cost = 40000
    elif plot_size < 800:
        kaasoo_cost = 45000
    elif plot_size < 900:
        kaasoo_cost = 50000
    elif plot_size < 1000:
        kaasoo_cost = 55000
    elif plot_size < 1200:
        kaasoo_cost = 80000
    elif plot_size < 1500:
        kaasoo_cost = 100000
    elif plot_size < 1800:
        kaasoo_cost = 120000
    elif plot_size < 2250:
        kaasoo_cost = 150000
    elif plot_size < 2500:
        kaasoo_cost = 180000
    elif plot_size < 2800:
        kaasoo_cost = 200000
    elif plot_size < 3200:
        kaasoo_cost = 250000
    elif plot_size < 3500:
        kaasoo_cost = 280000
    elif plot_size < 4000:
        kaasoo_cost = 300000
    else:
        kaasoo_cost = 350000

    # ---------------- Bajri ----------------
    bajri_qty = int(covered_area * settings.bajri_qty)
    bajri_cost = bajri_qty * settings.bajri_rate

    # ---------------- Cement ----------------
    cement_qty = int(covered_area * settings.cement_qty)
    cement_cost = cement_qty * settings.cement_rate

    # ---------------- Saria ----------------
    saria_qty = int(covered_area * settings.saria_qty)
    if saria_type == "Local 60G":
        saria_rate = settings.saria_local_rate
    else:
        saria_rate = settings.saria_branded_rate
    saria_cost = saria_qty * saria_rate

    # normalize bools
    termite_spray_required = str(termite_spray_required).lower() in ['yes', '1', 'true']
    pump_bore_required = str(pump_bore_required).lower() in ['yes', '1', 'true']

    # ---------------- Pump Bore ----------------
    pump_bore_qty, pump_bore_cost = 0, 0
    if pump_bore_required:
        pump_bore_qty = int(settings.pump_bore_qty)
        pump_bore_rate = settings.pump_bore_rate or 0
        pump_bore_cost = pump_bore_qty * pump_bore_rate

    # ---------------- Bijli Pipes ----------------
    if pipe_type == "Local":
        bijli_rate = settings.bijli_local_rate
    else:
        bijli_rate = settings.bijli_branded_rate
    bijli_cost = covered_area * bijli_rate

    # ---------------- Sanitary Pipes ----------------
    if sanitary_type == "Local":
        sanitary_rate = settings.sanitary_local_rate
    else:
        sanitary_rate = settings.sanitary_branded_rate
    sanitary_cost = covered_area * sanitary_rate

    # ---------------- Gate & Steel Chaukhat ----------------
    if gauge == "16 Gauge":
        gate_rate = settings.gate_16g_rate
    else:
        gate_rate = settings.gate_18g_rate
    gate_cost = covered_area * gate_rate

    # ---------------- Labour Cost ----------------
    labour_qty = int(covered_area)
    labour_rate = settings.labour_rate
    labour_cost = labour_qty * labour_rate

    # ---------------- Other Cost ----------------
    if covered_area < 500:
        other_cost = 30000
    elif covered_area < 600:
        other_cost = 35000
    elif covered_area < 700:
        other_cost = 40000
    elif covered_area < 800:
        other_cost = 45000
    elif covered_area < 900:
        other_cost = 50000
    elif covered_area < 1000:
        other_cost = 55000
    elif covered_area < 1200:
        other_cost = 80000
    elif covered_area < 1500:
        other_cost = 100000
    elif covered_area < 1800:
        other_cost = 120000
    elif covered_area < 2250:
        other_cost = 150000
    elif covered_area < 2500:
        other_cost = 180000
    elif covered_area < 2800:
        other_cost = 200000
    elif covered_area < 3200:
        other_cost = 250000
    elif covered_area < 3500:
        other_cost = 280000
    elif covered_area < 4000:
        other_cost = 300000
    else:
        other_cost = 350000

    # multiply by 1.5 to suit other_expenses
    other_cost = int(other_cost * 1.5)

    # ---------------- Total ----------------
    total_cost = (
        drawing_cost + foundation_cost + termite_cost +
        rori_cost + rait_ravi_cost + rait_chanab_cost +
        ent_cost + kaasoo_cost + bajri_cost + cement_cost +
        saria_cost + pump_bore_cost + bijli_cost + sanitary_cost +
        gate_cost + labour_cost + other_cost
    )

    # helper to format cost with commas
    def fmt(num):
        return f"{num:,.0f}"

    # ---------------- Details ----------------
    details = {}

    # 1. Foundation
    details["foundation"] = {
        "qty": f"{foundation_qty} SF",
        "rate": foundation_rate,
        "cost": fmt(foundation_cost)
    }

    # 2. Drawing
    if drawingRequired:
        details["drawing"] = {"cost": fmt(drawing_cost)}

    # 3. Termite
    if termite_spray_required:
        details["termite"] = {
            "qty": f"{spray_qty} SF",
            "rate": settings.termite_spray_rate,
            "cost": fmt(termite_cost)
        }

    # 4. Pump Bore
    if pump_bore_required:
        details["pump_bore"] = {"cost": fmt(pump_bore_cost)}

    # 5. Main Items
    details.update({
        "rori": {"qty": f"{rori_qty} SF", "rate": settings.rori_rate, "cost": fmt(rori_cost)},
        "rait_ravi": {"qty": f"{rait_ravi_qty} SF", "rate": settings.rait_ravi_rate, "cost": fmt(rait_ravi_cost)},
        "rait_chanab": {"qty": f"{rait_chanab_qty} SF", "rate": settings.rait_chanab_rate, "cost": fmt(rait_chanab_cost)},
        "ent": {"qty": f"{ent_qty} Pcs", "rate": ent_rate, "cost": fmt(ent_cost)},
        "bajri": {"qty": f"{bajri_qty} SF", "rate": settings.bajri_rate, "cost": fmt(bajri_cost)},
        "cement": {"qty": f"{cement_qty} Bags", "rate": settings.cement_rate, "cost": fmt(cement_cost)},
        "saria": {"qty": f"{saria_qty} KGs", "rate": saria_rate, "cost": fmt(saria_cost)},
        "labour": {"qty": f"{labour_qty} SF", "rate": labour_rate, "cost": fmt(labour_cost)},
        "kaasoo": {"cost": fmt(kaasoo_cost)},
    })

    # 6. Extra
    details["bijli"] = {"cost": fmt(bijli_cost)}
    details["sanitary"] = {"cost": fmt(sanitary_cost)}
    details["gate_chaukhat"] = {"cost": fmt(gate_cost)}

    # 7. Final
    details["other_expenses"] = {"cost": fmt(other_cost)}
    details["total_cost"] = fmt(total_cost)

    return total_cost, details
