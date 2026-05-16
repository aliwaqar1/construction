# Copyright (c) 2026, BuildCost Pro and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class AISettings(Document):
    pass


# ── Convenience accessors ──────────────────────────────────────────────────
# All other modules read AI configuration via these helpers so that secrets
# stay encrypted at rest and budget changes apply immediately.

_JOB_KEYS = {
    "floor_plan": {
        "vendor": "floor_plan_vendor",
        "model": "floor_plan_model",
        "max_cost_cents": "floor_plan_max_cost_cents",
        "daily_budget_cents": "floor_plan_daily_budget_cents",
        "monthly_budget_cents": "floor_plan_monthly_budget_cents",
        "rate_limit_per_min": "floor_plan_rate_limit_per_min",
    },
    "interior_design": {
        "vendor": "interior_vendor",
        "model": "interior_model",
        "max_cost_cents": "interior_max_cost_cents",
        "daily_budget_cents": "interior_daily_budget_cents",
        "monthly_budget_cents": "interior_monthly_budget_cents",
        "rate_limit_per_min": "interior_rate_limit_per_min",
    },
}


def get_settings():
    return frappe.get_cached_doc("AI Settings")


def get_api_key(vendor):
    """Return decrypted API key for a vendor (gemini, fal, anthropic, openai)."""
    doc = get_settings()
    field = f"{vendor}_api_key"
    try:
        return doc.get_password(field, raise_exception=False)
    except Exception:
        return None


def get_job_config(job_type):
    """Return per-job-type config as a plain dict."""
    keys = _JOB_KEYS.get(job_type)
    if not keys:
        raise ValueError(f"Unknown AI job_type: {job_type}")
    doc = get_settings()
    return {k: doc.get(field) for k, field in keys.items()}


def is_master_enabled():
    """False when admin disabled AI globally OR auto-tripped kill switch is active."""
    doc = get_settings()
    if not doc.enabled:
        return False
    if doc.kill_switch_until:
        from frappe.utils import now_datetime
        if now_datetime() < doc.kill_switch_until:
            return False
    return True


def trip_kill_switch(hours, reason=""):
    """Auto-disable AI for the next [hours]. Called when a budget cap is hit."""
    from datetime import timedelta
    from frappe.utils import now_datetime

    doc = frappe.get_doc("AI Settings")
    until = now_datetime() + timedelta(hours=hours)
    doc.kill_switch_until = until
    doc.save(ignore_permissions=True)
    frappe.db.commit()
    frappe.log_error(
        f"AI kill switch tripped until {until}. Reason: {reason}",
        "ai_settings.kill_switch",
    )
