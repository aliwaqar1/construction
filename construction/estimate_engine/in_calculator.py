"""
India V1 house-estimate calculator.

Uses city-based material rates from the existing ``Construction Material``
→ ``Material Rate`` → ``Material Master`` chain.

If no questionnaire answers are provided, defaults are used
(material_quality = "Medium").
"""

import frappe


def compute(city, plot_size_sqft, covered_area_sqft, params, floor_areas=None,
            user_rate_overrides=None, basement=None):
    """
    Compute an India house estimate.

    Parameters
    ----------
    city : str              – e.g. "Mumbai"
    plot_size_sqft : float  – (unused in V1 — kept for interface parity)
    covered_area_sqft : float
    params : dict           – merged parameter map from questionnaire impacts
    basement : dict | None  – (unused in V1 — kept for interface parity). The
                              basement's area is already inside
                              ``covered_area_sqft``, so it is priced as ordinary
                              floor area; the below-grade extras the PK engine
                              adds (excavation, retaining wall, tanking) have no
                              India rates yet.

    Note: ``floor_areas`` and ``user_rate_overrides`` are likewise accepted and
    ignored. ``v1.estimate`` reports ``overrides_applied`` off the premium check
    alone, so it would claim overrides this calculator never applied — the client
    hides the custom-rates card outside Pakistan, which is the only thing keeping
    that honest today.
    """
    material_quality = params.get("material_quality", "Medium")
    sqft = covered_area_sqft

    # Find the Construction Material document for this city + quality
    cm_docs = frappe.get_all(
        "Construction Material",
        filters={"city": city, "material_quality": material_quality},
        fields=["name"],
        limit=1,
    )

    # Fallback: try any quality for this city
    if not cm_docs:
        cm_docs = frappe.get_all(
            "Construction Material",
            filters={"city": city},
            fields=["name"],
            limit=1,
        )

    if not cm_docs:
        frappe.throw(f"No material rate data found for city '{city}'")

    cm_name = cm_docs[0]["name"]

    # Child table rows ordered by idx
    materials = frappe.get_all(
        "Material Rate",
        filters={"parent": cm_name},
        fields=["material", "uom", "rate"],
        order_by="idx asc",
    )

    line_items = []

    for m in materials:
        ratio = (
            frappe.db.get_value("Material Master", m["material"], "ratio_per_sqft")
            or 0
        )
        qty = ratio * sqft
        cost = qty * (m["rate"] or 0)

        line_items.append({
            "material": m["material"],
            "material_key": frappe.scrub(m["material"]),
            "phase": "combined",
            "qty": round(qty, 3),
            "unit": m["uom"] or "",
            "rate": float(m["rate"] or 0),
            "cost": round(cost, 2),
            "display": True,
        })

    # Total the ROUNDED line costs, the way pk_calculator does. Accumulating the
    # raw floats and rounding once at the end drifts away from the sum of what
    # the user is actually shown -- the breakdown then does not add up to its
    # own total (a paisa on a 12-item estimate, but it grows with the line count
    # and it is the sort of thing a contractor notices).
    total = round(sum(i["cost"] for i in line_items), 2)

    return {
        "currency": "INR",
        "line_items": line_items,
        "totals": {
            "overall": total,
        },
    }
