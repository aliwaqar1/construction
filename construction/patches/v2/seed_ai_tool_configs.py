# Seed default AI Tool Config rows + Feature Flag entries for all 10 tools.
# Idempotent.

import frappe


_TOOL_FLAGS = {
    "floor_plan": "ai_floor_plan",
    "interior":   "ai_interior",
    "exterior":   "ai_exterior",
    "garden":     "ai_garden",
    "layout":     "ai_layout",
    "cleanup":    "ai_cleanup",
    "ref":        "ai_ref",
    "paint":      "ai_paint",
    "replace":    "ai_replace",
    "floor":      "ai_floor",
}

# Defaults match _DEFAULT_TOOL_CONFIGS in ai_settings.py
_LEGACY_INTERIOR_DESIGN_FLAG = "ai_interior_design"


def execute():
    """Seed AI Tool Config rows and Feature Flag entries."""
    from construction.construction.doctype.ai_settings import ai_settings as ai_cfg

    if frappe.db.exists("DocType", "AI Settings"):
        ai_cfg.seed_default_tool_configs()

    if frappe.db.exists("DocType", "Feature Flag"):
        # Default state: enabled (so the app surfaces them). The admin can
        # flip a flag off without a redeploy.
        for tool_id, flag_key in _TOOL_FLAGS.items():
            if not frappe.db.exists("Feature Flag", flag_key):
                doc = frappe.new_doc("Feature Flag")
                doc.flag_key = flag_key
                doc.enabled = 1
                doc.description = f"Enables AI tool '{tool_id}'."
                try:
                    doc.insert(ignore_permissions=True)
                except Exception:
                    # If the doctype uses a different field name (e.g. `name`
                    # as the key), fall back to a name-only insert.
                    try:
                        frappe.get_doc({
                            "doctype": "Feature Flag",
                            "name": flag_key,
                            "enabled": 1,
                        }).insert(ignore_permissions=True)
                    except Exception:
                        frappe.log_error(
                            frappe.get_traceback(),
                            f"seed_ai: could not create flag {flag_key}",
                        )

        # Migrate legacy ai_interior_design flag to ai_interior (keep both
        # enabled so old clients still work).
        if frappe.db.exists("Feature Flag", _LEGACY_INTERIOR_DESIGN_FLAG):
            old = frappe.get_doc("Feature Flag", _LEGACY_INTERIOR_DESIGN_FLAG)
            if old.enabled and frappe.db.exists("Feature Flag", "ai_interior"):
                new = frappe.get_doc("Feature Flag", "ai_interior")
                if not new.enabled:
                    new.enabled = 1
                    new.save(ignore_permissions=True)

    frappe.db.commit()
