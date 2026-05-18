# Copyright (c) 2026, BuildCost Pro and contributors
# For license information, please see license.txt
#
# AI Settings — central config for all AI features.
# Per-tool config now lives in the `tool_configs` child table (AI Tool Config),
# one row per tool_id. Accessors below normalize that into a dict.

import frappe
from frappe.model.document import Document


class AISettings(Document):
    pass


# Defaults applied when AI Settings has no child row for a tool yet — used
# both by accessors and by the seeding helper (called from a patch).
_DEFAULT_TOOL_CONFIGS = {
    "floor_plan": {
        "vendor": "gemini",
        "model": "gemini-2.0-flash",
        "max_cost_cents": 5,
        "daily_budget_cents": 200,
        "monthly_budget_cents": 5000,
        "rate_limit_per_min": 5,
        "free_daily_limit": 5,
        "hd_supported": 0,
        "premium_only": 0,
        "enabled": 1,
    },
    "interior": {
        "vendor": "mock",
        "model": "gemini-2.5-flash-image",
        "max_cost_cents": 20,
        "daily_budget_cents": 111,
        "monthly_budget_cents": 2222,
        "rate_limit_per_min": 5,
        "free_daily_limit": 5,
        "hd_supported": 1,
        "premium_only": 0,
        "enabled": 1,
    },
    "exterior": {
        "vendor": "mock",
        "model": "gemini-2.5-flash-image",
        "max_cost_cents": 20,
        "daily_budget_cents": 111,
        "monthly_budget_cents": 2222,
        "rate_limit_per_min": 5,
        "free_daily_limit": 5,
        "hd_supported": 1,
        "premium_only": 0,
        "enabled": 1,
    },
    "garden": {
        "vendor": "mock",
        "model": "gemini-2.5-flash-image",
        "max_cost_cents": 20,
        "daily_budget_cents": 111,
        "monthly_budget_cents": 2222,
        "rate_limit_per_min": 5,
        "free_daily_limit": 5,
        "hd_supported": 1,
        "premium_only": 0,
        "enabled": 1,
    },
    "layout": {
        "vendor": "mock",
        "model": "gemini-2.5-flash-image",
        "max_cost_cents": 20,
        "daily_budget_cents": 111,
        "monthly_budget_cents": 2222,
        "rate_limit_per_min": 5,
        "free_daily_limit": 5,
        "hd_supported": 1,
        "premium_only": 0,
        "enabled": 1,
    },
    "cleanup": {
        "vendor": "mock",
        "model": "gemini-2.5-flash-image",
        "max_cost_cents": 20,
        "daily_budget_cents": 111,
        "monthly_budget_cents": 2222,
        "rate_limit_per_min": 5,
        "free_daily_limit": 5,
        "hd_supported": 1,
        "premium_only": 0,
        "enabled": 1,
    },
    "ref": {
        "vendor": "mock",
        "model": "gemini-2.5-flash-image",
        "max_cost_cents": 20,
        "daily_budget_cents": 111,
        "monthly_budget_cents": 2222,
        "rate_limit_per_min": 5,
        "free_daily_limit": 5,
        "hd_supported": 1,
        "premium_only": 0,
        "enabled": 1,
    },
    "paint": {
        "vendor": "mock",
        "model": "gemini-2.5-flash-image",
        "max_cost_cents": 20,
        "daily_budget_cents": 111,
        "monthly_budget_cents": 2222,
        "rate_limit_per_min": 5,
        "free_daily_limit": 5,
        "hd_supported": 1,
        "premium_only": 0,
        "enabled": 1,
    },
    "replace": {
        "vendor": "mock",
        "model": "gemini-2.5-flash-image",
        "max_cost_cents": 20,
        "daily_budget_cents": 111,
        "monthly_budget_cents": 2222,
        "rate_limit_per_min": 5,
        "free_daily_limit": 5,
        "hd_supported": 1,
        "premium_only": 0,
        "enabled": 1,
    },
    "floor": {
        "vendor": "mock",
        "model": "gemini-2.5-flash-image",
        "max_cost_cents": 20,
        "daily_budget_cents": 111,
        "monthly_budget_cents": 2222,
        "rate_limit_per_min": 5,
        "free_daily_limit": 5,
        "hd_supported": 1,
        "premium_only": 0,
        "enabled": 1,
    },
}


def get_settings():
    return frappe.get_cached_doc("AI Settings")


def get_api_key(vendor):
    """Return decrypted API key for a vendor (gemini, fal, anthropic, openai)."""
    if vendor == "mock":
        return "mock-key-no-spend"
    doc = get_settings()
    field = f"{vendor}_api_key"
    try:
        return doc.get_password(field, raise_exception=False)
    except Exception:
        return None


def get_job_config(tool_id):
    """Return per-tool config as a plain dict.

    Reads from the AI Settings.tool_configs child table; falls back to
    _DEFAULT_TOOL_CONFIGS when the row is absent (first boot, before the
    seed patch has run)."""
    if tool_id not in _DEFAULT_TOOL_CONFIGS:
        raise ValueError(f"Unknown AI tool_id: {tool_id}")

    doc = get_settings()
    for row in (doc.get("tool_configs") or []):
        if row.tool_id == tool_id:
            return {
                "tool_id": row.tool_id,
                "enabled": int(row.enabled or 0),
                "premium_only": int(row.premium_only or 0),
                "vendor": (row.vendor or "mock").lower(),
                "model": row.model or _DEFAULT_TOOL_CONFIGS[tool_id]["model"],
                "max_cost_cents": int(row.max_cost_cents or 0),
                "daily_budget_cents": int(row.daily_budget_cents or 0),
                "monthly_budget_cents": int(row.monthly_budget_cents or 0),
                "rate_limit_per_min": int(row.rate_limit_per_min or 0),
                "free_daily_limit": int(row.free_daily_limit or 0),
                "hd_supported": int(row.hd_supported or 0),
            }
    # No row yet — return defaults.
    return {"tool_id": tool_id, **_DEFAULT_TOOL_CONFIGS[tool_id]}


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


def seed_default_tool_configs():
    """Insert a default row per tool_id if the child table is empty.
    Idempotent — call from a patch or on app install."""
    doc = frappe.get_doc("AI Settings")
    existing = {row.tool_id for row in (doc.tool_configs or [])}
    added = 0
    for tool_id, defaults in _DEFAULT_TOOL_CONFIGS.items():
        if tool_id in existing:
            continue
        doc.append("tool_configs", {"tool_id": tool_id, **defaults})
        added += 1
    if added:
        doc.save(ignore_permissions=True)
        frappe.db.commit()
    return added


def is_premium(user):
    """True if the user has an active Premium Entry row."""
    if not user or user == 'Guest':
        return False
    doc = get_settings()
    from frappe.utils import now_datetime
    now = now_datetime()
    for row in (doc.get('premium_users') or []):
        if row.user != user:
            continue
        if not row.expires_at:
            return True
        if row.expires_at > now:
            return True
    return False
