# Seed placeholder basement-works rates on Construction Setting. Idempotent:
# only writes a field that is currently unset (None / 0), so an owner-tuned rate
# is never clobbered by a re-run.
#
# WARNING - THESE ARE PLACEHOLDERS, NOT SURVEYED RATES.
# A basement previously cost exactly what an above-grade storey cost: its area
# was folded into covered_area_sqft and none of the below-grade work was priced
# at all. These numbers make the line items appear and be roughly the right
# order of magnitude; they have NOT been checked against a real Lahore quote.
# Tune them in Construction Setting before launch (Desk > Construction Setting >
# Basement), the same way the India material rates are still an owner task.
#
#   basement_excavation_qty     10   -> cft of soil removed per sq ft of basement
#                                       floor, i.e. an effective 10 ft dig
#   basement_excavation_rate    30   -> PKR per cft removed
#   basement_wall_height_ft     10   -> retaining wall height
#   basement_retaining_rate     450  -> PKR per sq ft of retaining wall face
#   basement_waterproofing_rate 120  -> PKR per sq ft of slab + wall tanking

import frappe

_DEFAULTS = {
    "basement_excavation_qty": 10.0,
    "basement_excavation_rate": 30,
    "basement_wall_height_ft": 10.0,
    "basement_retaining_rate": 450,
    "basement_waterproofing_rate": 120,
}


def execute():
    if not frappe.db.exists("DocType", "Construction Setting"):
        return

    doc = frappe.get_single("Construction Setting")
    changed = False
    for field, default in _DEFAULTS.items():
        if not doc.get(field):
            doc.set(field, default)
            changed = True

    if changed:
        doc.save(ignore_permissions=True)
        frappe.db.commit()
