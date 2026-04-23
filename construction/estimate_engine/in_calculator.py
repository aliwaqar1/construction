"""
India V1 house-estimate calculator.

Uses city-based material rates from the existing ``Construction Material``
→ ``Material Rate`` → ``Material Master`` chain.

If no questionnaire answers are provided, defaults are used
(material_quality = "Medium").
"""

import frappe


def compute(city, plot_size_sqft, covered_area_sqft, params):
    """
    Compute an India house estimate.

    Parameters
    ----------
    city : str              – e.g. "Mumbai"
    plot_size_sqft : float  – (unused in V1 — kept for interface parity)
    covered_area_sqft : float
    params : dict           – merged parameter map from questionnaire impacts
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
    total = 0.0

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
        })
        total += cost

    return {
        "line_items": line_items,
        "totals": {
            "overall": round(total, 2),
        },
    }
