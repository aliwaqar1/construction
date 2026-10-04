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
        "model": "gemini-3.8-flash",
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


# A tripped TOOL is paused in the cache rather than written to the config
# row: it must expire on its own, and it must not require an admin to
# remember to turn it back on.
_TOOL_TRIP_KEY = "ai_tool_tripped"
_BUDGET_PAUSE_KEY = "ai_budget_paused"


def trip_tool_breaker(tool_id, hours, reason=""):
    """Take ONE tool offline for `hours`.

    The circuit breaker counts failures per tool and then used to trip the
    GLOBAL kill switch, so six failures on the least-used tool took the entire
    product offline — including for subscribers, whose credits then became
    unspendable because the gate sits upstream of the debit. Scope the
    response to the thing that is actually failing.
    """
    try:
        frappe.cache().set_value(
            f"{_TOOL_TRIP_KEY}:{tool_id}", reason or "1",
            expires_in_sec=int(max(1, hours * 3600)),
        )
    except Exception:
        # Cache down — fall back to the global switch rather than keep calling
        # a vendor that is failing.
        trip_kill_switch(hours=hours, reason=f"[cache down] {reason}")
        return
    frappe.log_error(
        f"AI tool {tool_id} tripped for {hours}h. Reason: {reason}",
        "ai_settings.tool_breaker",
    )


def is_tool_tripped(tool_id):
    """True while `tool_id` is inside its circuit-breaker cool-down."""
    try:
        return bool(frappe.cache().get_value(f"{_TOOL_TRIP_KEY}:{tool_id}"))
    except Exception:
        return False


def clear_tool_breaker(tool_id):
    try:
        frappe.cache().delete_value(f"{_TOOL_TRIP_KEY}:{tool_id}")
    except Exception:
        pass


def pause_free_tier(scope, hours, reason=""):
    """Stop FREE work for `hours` because a budget cap was reached.

    Not the global kill switch. A budget breach is a cost problem; killing the
    product converts it into an availability problem for people who have
    already paid, and their credits become unspendable. Free generations are
    what the cap exists to bound, so free generations are what stop.
    """
    try:
        frappe.cache().set_value(
            f"{_BUDGET_PAUSE_KEY}:{scope}", reason or "1",
            expires_in_sec=int(max(1, hours * 3600)),
        )
    except Exception:
        trip_kill_switch(hours=hours, reason=f"[cache down] {reason}")
        return
    frappe.log_error(
        f"Free-tier AI paused for {hours}h ({scope}). Reason: {reason}",
        "ai_settings.budget_paused",
    )


def free_tier_paused(scope):
    try:
        return bool(frappe.cache().get_value(f"{_BUDGET_PAUSE_KEY}:{scope}"))
    except Exception:
        return False


def trip_kill_switch(hours, reason=""):
    """Auto-disable AI for the next [hours], GLOBALLY.

    Reserve this for situations where continuing to serve anyone is the wrong
    answer — a vendor outage the cache can't scope, or a manual intervention.
    Budget breaches use `pause_free_tier` and per-tool failures use
    `trip_tool_breaker`; neither should take the product away from subscribers.
    """
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


def verify_live_tool_configs():
    """Config-drift smoke test (B4), run daily from hooks.py.

    The in-code defaults above still say vendor="mock" for the image tools,
    so a fresh site, a wiped child table, or a re-run seed patch would
    silently serve placeholder images in production. This asserts every
    enabled tool points at a real vendor WITH an API key present, and logs
    loudly when anything has drifted back.

    Dev sites that intentionally run on mock can set
    `ai_allow_mock_vendor: 1` in site_config.json to silence it.
    Returns the list of problems (empty = healthy) so it can also be called
    ad-hoc from `bench execute` as a deploy-time check."""
    if frappe.conf.get("ai_allow_mock_vendor"):
        return []

    doc = get_settings()
    problems = []
    for tool_id in _DEFAULT_TOOL_CONFIGS:
        cfg = get_job_config(tool_id)
        if not cfg.get("enabled"):
            continue
        vendor = (cfg.get("vendor") or "mock").lower()
        if vendor == "mock":
            problems.append(
                f"{tool_id}: vendor is 'mock' — users are getting placeholder "
                "images, not real AI output"
            )
            continue
        if not get_api_key(vendor):
            problems.append(
                f"{tool_id}: vendor '{vendor}' has no API key configured — "
                "every run will fail"
            )
    if not doc.enabled:
        problems.append("AI Settings.enabled is off — all AI tools are dark")

    if problems:
        frappe.log_error(
            "AI config drift detected:\n" + "\n".join(problems),
            "ai_settings.config_drift",
        )
    return problems


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
