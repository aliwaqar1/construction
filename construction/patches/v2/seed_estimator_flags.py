# Seed Feature Flag rows for the estimator kill switches. Idempotent.
#
# estimator_enabled       - master switch for the whole estimator flow
# estimator_live_preview  - the debounced LIVE-bar preview (one full backend
#                           estimate per debounce); flip off to shed load
#                           while keeping Calculate working.
# Both ship enabled; the client also defaults them ON when /v1/config is
# unreachable, so they act purely as remote kill switches.

import frappe

_FLAGS = {
    "estimator_enabled": "Master switch for the estimator flow.",
    "estimator_live_preview": "Debounced live estimate preview in the stepper.",
}


def execute():
    if not frappe.db.exists("DocType", "Feature Flag"):
        return
    for flag_key, description in _FLAGS.items():
        if frappe.db.exists("Feature Flag", flag_key):
            continue
        doc = frappe.new_doc("Feature Flag")
        doc.flag_key = flag_key
        doc.enabled = 1
        doc.description = description
        doc.insert(ignore_permissions=True)
    frappe.db.commit()
