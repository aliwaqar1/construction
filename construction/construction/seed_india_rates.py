"""Seed starter India material-rate data so the India estimate flow works.

QA 1.12: India estimates 500'd because there were **zero** `Construction
Material` rows — `in_calculator.compute` throws when a city has no rate data.

This seeds:
  * `Material Master` entries (with `ratio_per_sqft`)
  * `Construction Material` docs for each India city × quality, each with a
    `Material Rate` child row per material.

⚠️  RATES ARE STARTER PLACEHOLDERS. They produce believable per-sqft totals
    (Basic ≈ ₹1,440 · Medium ≈ ₹1,690 · Premium ≈ ₹2,200) but MUST be replaced
    with validated market rates before go-live.

Run idempotently:
    bench --site construction.local execute \
        construction.construction.seed_india_rates.run
"""

import frappe

# Material Master is autonamed `field:material_type`, so the type label IS the
# unique material id (and what Material Rate.material links to). Keep each label
# unique.
# (material_type, uom, ratio_per_sqft, base_medium_rate_inr)
MATERIALS = [
    ("Cement",            "Bag",  0.40, 420),
    ("Sand",              "cft",  1.80,  75),
    ("Aggregate",         "cft",  1.35,  65),
    ("Steel (TMT)",       "kg",   4.00,  70),
    ("Bricks",            "Nos",  8.00,   9),
    ("Flooring",          "sqft", 1.00, 120),
    ("Paint",             "sqft", 1.00,  55),
    ("Electrical",        "sqft", 1.00, 110),
    ("Plumbing",          "sqft", 1.00,  95),
    ("Doors & Windows",   "sqft", 1.00, 130),
    ("Labour",            "sqft", 1.00, 350),
    ("Finishing",         "sqft", 1.00,  90),
]

# City cost multipliers (metros run a little higher).
CITY_MULT = {
    "Mumbai": 1.15,
    "Delhi": 1.08,
    "Bangalore": 1.05,
    "Hyderabad": 1.00,
    "Chennai": 1.00,
}

# Quality multipliers — keys must match the Select options on Construction
# Material (`Basic` / `Medium` / `Premium`).
QUALITY_MULT = {
    "Basic": 0.85,
    "Medium": 1.00,
    "Premium": 1.30,
}


def _ensure_materials():
    for mtype, uom, ratio, _rate in MATERIALS:
        if frappe.db.exists("Material Master", mtype):
            doc = frappe.get_doc("Material Master", mtype)
            doc.uom = uom
            doc.ratio_per_sqft = ratio
            doc.save(ignore_permissions=True)
        else:
            frappe.get_doc({
                "doctype": "Material Master",
                "material_type": mtype,
                "uom": uom,
                "ratio_per_sqft": ratio,
            }).insert(ignore_permissions=True)


def _seed_city(city, quality):
    # Replace any existing doc for this city+quality so re-runs stay clean.
    for existing in frappe.get_all(
        "Construction Material",
        filters={"city": city, "material_quality": quality},
        pluck="name",
    ):
        frappe.delete_doc("Construction Material", existing, ignore_permissions=True, force=True)

    qmult = QUALITY_MULT[quality]
    cmult = CITY_MULT.get(city, 1.0)
    rows = []
    for mtype, uom, _ratio, base_rate in MATERIALS:
        rows.append({
            "material": mtype,
            "uom": uom,
            "rate": round(base_rate * qmult * cmult, 2),
            "notes": "Starter placeholder rate — validate before go-live.",
        })

    frappe.get_doc({
        "doctype": "Construction Material",
        "city": city,
        "material_quality": quality,
        "area_unit": "sqft",
        "materials": rows,
        "last_updated": frappe.utils.now_datetime(),
    }).insert(ignore_permissions=True)


def _clean():
    """Drop any prior starter rows so a re-run starts from a clean slate."""
    for cm in frappe.get_all("Construction Material", pluck="name"):
        frappe.delete_doc("Construction Material", cm, ignore_permissions=True, force=True)
    for mm in frappe.get_all("Material Master", pluck="name"):
        frappe.delete_doc("Material Master", mm, ignore_permissions=True, force=True)


def run():
    _clean()
    _ensure_materials()
    for city in CITY_MULT:
        for quality in QUALITY_MULT:
            _seed_city(city, quality)
    frappe.db.commit()
    count = frappe.db.count("Construction Material")
    print(f"Seeded India rates: {count} Construction Material docs, "
          f"{len(MATERIALS)} materials.")
