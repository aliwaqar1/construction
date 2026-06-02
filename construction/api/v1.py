"""
V1 API endpoints for the questionnaire-driven estimate engine.

Frappe URLs (all allow_guest):
    GET  /api/method/construction.api.v1.countries
    GET  /api/method/construction.api.v1.cities?country=PK
    GET  /api/method/construction.api.v1.questionnaire?country=PK&city=Lahore&flow=house_estimate_v1
    POST /api/method/construction.api.v1.estimate   {country, city, plot_size_sqft, covered_area_sqft, answers:{...,floor_areas:[gf,1f,...]}}
"""

import hashlib
import json
import sys
import traceback
import uuid

import frappe

from construction.estimate_engine.engine import resolve_estimate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _error_response(code, message, http_status=400, details=None, correlation_id=None):
    """Return a structured error dict and set the HTTP status code.

    Pass `correlation_id` (from `_log_unhandled`) so the client can surface
    it for support and tie back to the server-side Error Log row.
    """
    frappe.local.response.http_status_code = http_status
    resp = {
        "ok": False,
        "error": {
            "code": code,
            "message": message,
        },
    }
    if correlation_id:
        resp["error"]["correlation_id"] = correlation_id
    if details:
        resp["error"]["details"] = details
    return resp


def _log_unhandled(method_name):
    """Record an unhandled backend exception in App Error Log; return its
    correlation id.

    The id goes into the API response so the client can show a Report
    affordance carrying it, and is stored on the App Error Log row so a search
    finds the matching record instantly.

    The current request transaction is rolled back first: it errored, so its
    partial writes must be discarded, and a clean transaction lets us persist
    the error row reliably. Falls back to Frappe's Error Log only if writing
    our own row fails, so an error is never lost entirely.
    """
    corr_id = uuid.uuid4().hex[:12]
    tb = traceback.format_exc()
    exc_type = ""
    try:
        et = sys.exc_info()[0]
        exc_type = et.__name__ if et else ""
    except Exception:
        pass
    try:
        frappe.db.rollback()
        from construction.construction.doctype.app_error_log.app_error_log import (
            AppErrorLog,
        )
        AppErrorLog.record(
            source="backend",
            level="error",
            is_fatal=False,
            title=f"{method_name} \u00b7 {corr_id}",
            message=method_name,
            error_type=exc_type,
            stack_trace=tb,
            route=method_name,
            endpoint=method_name,
            correlation_id=corr_id,
            environment="production",
            user=getattr(getattr(frappe, "session", None), "user", None),
        )
        frappe.db.commit()
    except Exception:
        # Last resort so the trace is never lost.
        try:
            frappe.log_error(tb, f"{method_name} \u00b7 {corr_id}")
        except Exception:
            pass
    return corr_id


# ---------------------------------------------------------------------------
# GET /v1/countries
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=True)
def countries():
    """Return all enabled estimate countries."""
    try:
        rows = frappe.get_all(
            "Estimate Country",
            filters={"enabled": 1},
            fields=["code", "country_name", "currency"],
            order_by="country_name asc",
        )
        return {"countries": rows}
    except Exception:
        corr = _log_unhandled("v1.countries")
        return _error_response("SERVER_ERROR", "Failed to load countries", 500, correlation_id=corr)


# ---------------------------------------------------------------------------
# GET /v1/cities?country=PK
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=True)
def cities(country=None):
    """Return enabled cities, optionally filtered by country code."""
    try:
        filters = {"enabled": 1}
        if country:
            filters["country_code"] = country

        rows = frappe.get_all(
            "Estimate City",
            filters=filters,
            fields=["city_name", "country_code"],
            order_by="city_name asc",
        )
        return {"cities": rows}
    except Exception:
        corr = _log_unhandled("v1.cities")
        return _error_response("SERVER_ERROR", "Failed to load cities", 500, correlation_id=corr)


# ---------------------------------------------------------------------------
# GET /v1/questionnaire?country=PK&city=Lahore&flow=house_estimate_v1
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=True)
def questionnaire(country=None, city=None, flow=None):
    """Return the questionnaire definition for a country + flow."""
    if not country or not flow:
        return _error_response(
            "MISSING_PARAMS",
            "'country' and 'flow' are required query parameters",
        )

    try:
        docs = frappe.get_all(
            "Estimate Questionnaire",
            filters={"country": country, "flow_key": flow},
            fields=["flow_key", "country", "version", "title", "description", "steps_json"],
            limit=1,
        )

        if not docs:
            return _error_response(
                "NOT_FOUND",
                f"No questionnaire found for country={country}, flow={flow}",
                404,
            )

        doc = docs[0]
        steps_data = json.loads(doc.steps_json) if doc.steps_json else {"steps": []}

        return {
            "flow_key": doc.flow_key,
            "country": doc.country,
            "version": doc.version,
            "title": doc.title,
            "description": doc.description,
            "steps": steps_data.get("steps", []),
        }
    except Exception:
        corr = _log_unhandled("v1.questionnaire")
        return _error_response("SERVER_ERROR", "Failed to load questionnaire", 500, correlation_id=corr)


# ---------------------------------------------------------------------------
# POST /v1/estimate
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=True, methods=["POST"])
def estimate(**kwargs):
    """Compute a construction estimate."""
    # ── Parse request body ──
    data = kwargs
    if frappe.request and frappe.request.is_json:
        raw_data = frappe.request.data
        if isinstance(raw_data, bytes):
            raw_data = raw_data.decode("utf-8")

        try:
            parsed_data = frappe.parse_json(raw_data)
        except Exception:
            return _error_response("INVALID_JSON", "Request body is not valid JSON")

        if isinstance(parsed_data, dict):
            data = parsed_data
        else:
            return _error_response("INVALID_JSON", "Request body must be a JSON object")

    country = data.get("country")
    city = data.get("city")
    plot_size = data.get("plot_size_sqft")
    covered_area = data.get("covered_area_sqft")
    answers = data.get("answers") or {}

    if isinstance(answers, str):
        try:
            answers = frappe.parse_json(answers)
        except Exception:
            return _error_response("INVALID_PARAMS", "'answers' is not valid JSON")

    # ── Validation ──
    missing = []
    if not country:
        missing.append("country")
    if not city:
        missing.append("city")
    if plot_size is None:
        missing.append("plot_size_sqft")
    if covered_area is None:
        missing.append("covered_area_sqft")

    if missing:
        return _error_response(
            "MISSING_PARAMS",
            f"Missing required fields: {', '.join(missing)}",
            details={"fields": missing},
        )

    try:
        plot_size = float(plot_size)
        covered_area = float(covered_area)
    except (TypeError, ValueError):
        return _error_response(
            "INVALID_PARAMS",
            "'plot_size_sqft' and 'covered_area_sqft' must be numeric",
        )

    if plot_size <= 0 or covered_area <= 0:
        return _error_response(
            "INVALID_PARAMS",
            "'plot_size_sqft' and 'covered_area_sqft' must be positive",
        )

    if not isinstance(answers, dict):
        return _error_response("INVALID_PARAMS", "'answers' must be a JSON object")

    # ── Compute ──
    try:
        result = resolve_estimate(country, city, plot_size, covered_area, answers)
        return result
    except frappe.ValidationError as e:
        return _error_response("VALIDATION_ERROR", str(e), 422)
    except Exception:
        corr = _log_unhandled("v1.estimate")
        return _error_response(
            "SERVER_ERROR",
            "An unexpected error occurred while computing the estimate",
            500,
            correlation_id=corr,
        )


# ---------------------------------------------------------------------------
# GET /v1/config?app_version=1.0.0&device_id=...
# ---------------------------------------------------------------------------


def _flag_active_for_device(flag, device_id, app_version):
    """Decide whether a flag is on for a given device."""
    if not flag.get("enabled"):
        return False

    min_v = (flag.get("min_app_version") or "").strip()
    if min_v and app_version:
        try:
            cur = tuple(int(p) for p in app_version.split("."))
            req = tuple(int(p) for p in min_v.split("."))
            if cur < req:
                return False
        except ValueError:
            pass

    rollout = flag.get("rollout_percentage")
    if rollout is None:
        rollout = 100
    rollout = max(0, min(100, int(rollout)))
    if rollout >= 100:
        return True
    if rollout <= 0:
        return False

    if not device_id:
        return False
    bucket = int(hashlib.md5(f"{flag['flag_key']}:{device_id}".encode()).hexdigest(), 16) % 100
    return bucket < rollout


@frappe.whitelist(allow_guest=True)
def config(app_version=None, device_id=None):
    """Return feature flags + kill switches for the client.

    Clients should call this on app start (and periodically thereafter)
    and cache the result. Flags are evaluated server-side per device.
    """
    try:
        rows = frappe.get_all(
            "Feature Flag",
            fields=["flag_key", "enabled", "min_app_version", "rollout_percentage"],
            order_by="flag_key asc",
        )
        flags = {r["flag_key"]: _flag_active_for_device(r, device_id, app_version) for r in rows}
        return {
            "flags": flags,
            "min_supported_version": "1.0.0",
        }
    except Exception:
        corr = _log_unhandled("v1.config")
        return _error_response("SERVER_ERROR", "Failed to load config", 500, correlation_id=corr)


# ============================================================================
# PHASE 2 — AUTH
# ============================================================================
#
# Frappe issues api_key + api_secret per User. We expose a thin login wrapper
# that takes username + password and returns the keys. Subsequent calls use
# header: Authorization: token api_key:api_secret
# ============================================================================


def _user_keys(user):
    """Return (api_key, api_secret) for a User, generating them on demand."""
    user_doc = frappe.get_doc("User", user)
    if not user_doc.api_key:
        user_doc.api_key = frappe.generate_hash(length=15)
    api_secret = frappe.generate_hash(length=15)
    user_doc.api_secret = api_secret
    user_doc.save(ignore_permissions=True)
    return user_doc.api_key, api_secret


@frappe.whitelist(allow_guest=True, methods=["POST"])
def login(usr=None, pwd=None):
    """Authenticate a user and return api_key/api_secret.

    Body: {"usr": "email", "pwd": "password"}
    """
    data = frappe.local.form_dict if frappe.local.form_dict else {}
    if frappe.request and frappe.request.is_json:
        try:
            raw = frappe.request.data
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            data = frappe.parse_json(raw) or {}
        except Exception:
            return _error_response("INVALID_JSON", "Body is not valid JSON")

    usr = usr or data.get("usr") or data.get("email")
    pwd = pwd or data.get("pwd") or data.get("password")

    if not usr or not pwd:
        return _error_response("MISSING_PARAMS", "'usr' and 'pwd' are required")

    try:
        from frappe.auth import LoginManager
        lm = LoginManager()
        lm.authenticate(user=usr, pwd=pwd)
        lm.post_login()
    except frappe.AuthenticationError:
        return _error_response("INVALID_CREDENTIALS", "Invalid email or password", 401)
    except Exception:
        corr = _log_unhandled("v1.login")
        return _error_response("SERVER_ERROR", "Login failed", 500, correlation_id=corr)

    api_key, api_secret = _user_keys(frappe.session.user)
    return {
        "user": frappe.session.user,
        "api_key": api_key,
        "api_secret": api_secret,
        "full_name": frappe.db.get_value("User", frappe.session.user, "full_name"),
    }


@frappe.whitelist(allow_guest=True, methods=["POST"])
def register(email=None, password=None, full_name=None):
    """Create a new user account. Returns api_key/secret on success."""
    data = {}
    if frappe.request and frappe.request.is_json:
        try:
            raw = frappe.request.data
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            data = frappe.parse_json(raw) or {}
        except Exception:
            return _error_response("INVALID_JSON", "Body is not valid JSON")

    email = email or data.get("email")
    password = password or data.get("password")
    full_name = full_name or data.get("full_name") or ""

    if not email or not password:
        return _error_response("MISSING_PARAMS", "'email' and 'password' are required")

    if len(password) < 8:
        return _error_response("INVALID_PARAMS", "Password must be at least 8 characters")

    if frappe.db.exists("User", email):
        return _error_response("USER_EXISTS", "An account with that email already exists", 409)

    try:
        first, _, last = full_name.partition(" ")
        user_doc = frappe.get_doc({
            "doctype": "User",
            "email": email,
            "first_name": first or email.split("@")[0],
            "last_name": last or "",
            "send_welcome_email": 0,
            "enabled": 1,
            "new_password": password,
            "user_type": "Website User",
        })
        user_doc.flags.ignore_permissions = True
        user_doc.insert(ignore_permissions=True)
        frappe.db.commit()
    except Exception:
        corr = _log_unhandled("v1.register")
        return _error_response("SERVER_ERROR", "Failed to create account", 500, correlation_id=corr)

    api_key, api_secret = _user_keys(email)
    return {
        "user": email,
        "api_key": api_key,
        "api_secret": api_secret,
        "full_name": full_name,
    }


@frappe.whitelist()
def me():
    """Return the current authenticated user's profile."""
    user = frappe.session.user
    if user == "Guest":
        return _error_response("UNAUTHORIZED", "Authentication required", 401)
    return {
        "user": user,
        "full_name": frappe.db.get_value("User", user, "full_name"),
    }


# ============================================================================
# PHASE 2 - SERVER ESTIMATES (save / list / get / delete / sync / compare)
# ============================================================================


def _require_auth():
    if frappe.session.user == "Guest":
        frappe.throw("Authentication required", frappe.AuthenticationError)


def _estimate_to_dict(name):
    doc = frappe.get_doc("Estimate History", name)
    if doc.user != frappe.session.user:
        frappe.throw("Forbidden", frappe.PermissionError)
    return {
        "name": doc.name,
        "client_id": doc.client_id,
        "country": doc.country,
        "city": doc.city,
        "plot_size_sqft": doc.plot_size_sqft,
        "covered_area_sqft": doc.covered_area_sqft,
        "quality_level": doc.quality_level,
        "grand_total": doc.grand_total,
        "currency": doc.currency,
        "answers": json.loads(doc.answers_json or "{}"),
        "snapshot": json.loads(doc.snapshot_json or "{}"),
        "saved_at": str(doc.saved_at) if doc.saved_at else None,
    }


def _read_json_body():
    if frappe.request and frappe.request.is_json:
        try:
            raw = frappe.request.data
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            return frappe.parse_json(raw) or {}
        except Exception:
            return None
    return frappe.local.form_dict if frappe.local.form_dict else {}


@frappe.whitelist(methods=["POST"])
def save_estimate(**kwargs):
    """Persist a single estimate. Idempotent on client_id."""
    try:
        _require_auth()
    except frappe.AuthenticationError:
        return _error_response("UNAUTHORIZED", "Authentication required", 401)

    data = _read_json_body()
    if data is None:
        return _error_response("INVALID_JSON", "Body is not valid JSON")

    client_id = data.get("client_id")
    if not client_id:
        return _error_response("MISSING_PARAMS", "'client_id' is required")

    snapshot = data.get("snapshot") or {}
    answers = data.get("answers") or {}

    try:
        existing = frappe.db.get_value(
            "Estimate History",
            {"client_id": client_id, "user": frappe.session.user},
            "name",
        )
        if existing:
            doc = frappe.get_doc("Estimate History", existing)
        else:
            doc = frappe.new_doc("Estimate History")
            doc.client_id = client_id
            doc.user = frappe.session.user

        doc.country = data.get("country")
        doc.city = data.get("city")
        doc.plot_size_sqft = data.get("plot_size_sqft") or 0
        doc.covered_area_sqft = data.get("covered_area_sqft") or 0
        doc.quality_level = data.get("quality_level")
        doc.grand_total = data.get("grand_total") or 0
        doc.currency = data.get("currency") or "PKR"
        doc.answers_json = json.dumps(answers)
        doc.snapshot_json = json.dumps(snapshot)
        doc.saved_at = data.get("saved_at") or frappe.utils.now_datetime()
        doc.save(ignore_permissions=True)
        frappe.db.commit()
        return {"ok": True, "name": doc.name, "client_id": doc.client_id}
    except Exception:
        corr = _log_unhandled("v1.save_estimate")
        return _error_response("SERVER_ERROR", "Failed to save estimate", 500, correlation_id=corr)


@frappe.whitelist()
def list_estimates(limit=50, offset=0):
    try:
        _require_auth()
    except frappe.AuthenticationError:
        return _error_response("UNAUTHORIZED", "Authentication required", 401)

    try:
        rows = frappe.get_all(
            "Estimate History",
            filters={"user": frappe.session.user},
            fields=[
                "name",
                "client_id",
                "country",
                "city",
                "plot_size_sqft",
                "covered_area_sqft",
                "quality_level",
                "grand_total",
                "currency",
                "saved_at",
            ],
            order_by="saved_at desc",
            limit_start=int(offset),
            limit_page_length=int(limit),
        )
        for r in rows:
            r["saved_at"] = str(r["saved_at"]) if r.get("saved_at") else None
        return {"estimates": rows}
    except Exception:
        corr = _log_unhandled("v1.list_estimates")
        return _error_response("SERVER_ERROR", "Failed to list estimates", 500, correlation_id=corr)


@frappe.whitelist()
def get_estimate(name=None):
    try:
        _require_auth()
    except frappe.AuthenticationError:
        return _error_response("UNAUTHORIZED", "Authentication required", 401)

    if not name:
        return _error_response("MISSING_PARAMS", "'name' is required")

    if not frappe.db.exists("Estimate History", name):
        return _error_response("NOT_FOUND", "Estimate not found", 404)

    try:
        return _estimate_to_dict(name)
    except frappe.PermissionError:
        return _error_response("FORBIDDEN", "You do not own this estimate", 403)


@frappe.whitelist(methods=["POST"])
def delete_estimate(name=None):
    try:
        _require_auth()
    except frappe.AuthenticationError:
        return _error_response("UNAUTHORIZED", "Authentication required", 401)

    data = _read_json_body() or {}
    name = name or data.get("name")
    if not name:
        return _error_response("MISSING_PARAMS", "'name' is required")

    if not frappe.db.exists("Estimate History", name):
        return _error_response("NOT_FOUND", "Estimate not found", 404)

    owner = frappe.db.get_value("Estimate History", name, "user")
    if owner != frappe.session.user:
        return _error_response("FORBIDDEN", "You do not own this estimate", 403)

    try:
        frappe.delete_doc("Estimate History", name, ignore_permissions=True)
        frappe.db.commit()
        return {"ok": True}
    except Exception:
        corr = _log_unhandled("v1.delete_estimate")
        return _error_response("SERVER_ERROR", "Failed to delete estimate", 500, correlation_id=corr)


@frappe.whitelist(methods=["POST"])
def sync_estimates(**kwargs):
    """Push a list of local estimates, pull the server list back.

    Body: {"estimates": [<same shape as save_estimate>...]}
    """
    try:
        _require_auth()
    except frappe.AuthenticationError:
        return _error_response("UNAUTHORIZED", "Authentication required", 401)

    data = _read_json_body()
    if data is None:
        return _error_response("INVALID_JSON", "Body is not valid JSON")

    incoming = data.get("estimates") or []
    if not isinstance(incoming, list):
        return _error_response("INVALID_PARAMS", "'estimates' must be a list")

    saved = []
    failed = []
    for est in incoming:
        if not isinstance(est, dict) or not est.get("client_id"):
            failed.append({"reason": "missing client_id", "data": est})
            continue
        try:
            existing = frappe.db.get_value(
                "Estimate History",
                {"client_id": est["client_id"], "user": frappe.session.user},
                "name",
            )
            if existing:
                doc = frappe.get_doc("Estimate History", existing)
            else:
                doc = frappe.new_doc("Estimate History")
                doc.client_id = est["client_id"]
                doc.user = frappe.session.user
            doc.country = est.get("country")
            doc.city = est.get("city")
            doc.plot_size_sqft = est.get("plot_size_sqft") or 0
            doc.covered_area_sqft = est.get("covered_area_sqft") or 0
            doc.quality_level = est.get("quality_level")
            doc.grand_total = est.get("grand_total") or 0
            doc.currency = est.get("currency") or "PKR"
            doc.answers_json = json.dumps(est.get("answers") or {})
            doc.snapshot_json = json.dumps(est.get("snapshot") or {})
            doc.saved_at = est.get("saved_at") or frappe.utils.now_datetime()
            doc.save(ignore_permissions=True)
            saved.append(doc.client_id)
        except Exception:
            corr = _log_unhandled("v1.sync_estimates.item")
            failed.append({
                "client_id": est.get("client_id"),
                "reason": "server_error",
                "correlation_id": corr,
            })

    frappe.db.commit()

    server_rows = frappe.get_all(
        "Estimate History",
        filters={"user": frappe.session.user},
        fields=["name", "client_id", "city", "country", "grand_total", "currency", "saved_at"],
        order_by="saved_at desc",
    )
    for r in server_rows:
        r["saved_at"] = str(r["saved_at"]) if r.get("saved_at") else None

    return {"saved": saved, "failed": failed, "server": server_rows}


@frappe.whitelist()
def compare_estimates(a=None, b=None):
    """Diff two saved estimates by name. Returns totals + line-item changes."""
    try:
        _require_auth()
    except frappe.AuthenticationError:
        return _error_response("UNAUTHORIZED", "Authentication required", 401)

    if not a or not b:
        return _error_response("MISSING_PARAMS", "'a' and 'b' are required")

    if not frappe.db.exists("Estimate History", a) or not frappe.db.exists("Estimate History", b):
        return _error_response("NOT_FOUND", "Estimate not found", 404)

    try:
        ea = _estimate_to_dict(a)
        eb = _estimate_to_dict(b)
    except frappe.PermissionError:
        return _error_response("FORBIDDEN", "You do not own one of these estimates", 403)

    def _items(snapshot):
        return snapshot.get("line_items") or []

    items_a = {i.get("material_key") or i.get("material"): i for i in _items(ea["snapshot"])}
    items_b = {i.get("material_key") or i.get("material"): i for i in _items(eb["snapshot"])}

    diffs = []
    for key in sorted(set(items_a) | set(items_b)):
        ia = items_a.get(key)
        ib = items_b.get(key)
        diffs.append({
            "material": (ia or ib).get("material"),
            "a": ia,
            "b": ib,
            "delta": (ib.get("cost", 0) if ib else 0) - (ia.get("cost", 0) if ia else 0),
        })

    return {
        "a": {"name": ea["name"], "city": ea["city"], "grand_total": ea["grand_total"], "saved_at": ea["saved_at"]},
        "b": {"name": eb["name"], "city": eb["city"], "grand_total": eb["grand_total"], "saved_at": eb["saved_at"]},
        "total_delta": (eb["grand_total"] or 0) - (ea["grand_total"] or 0),
        "diffs": diffs,
    }


# ============================================================================

# ============================================================================
# PHASE 2 - AI (all 9 design tools + floor plan)
# ============================================================================
#
# Architecture:
#   client -> POST ai_generate (multipart: image + tool_id + style + ...)
#          -> AI Job doc created (status=queued) -> RQ worker
#          -> client polls /job_status until done|failed
#          -> client optionally POSTs ai_save_design to persist a result
#
# Kill switches: Feature Flag flag_key per tool (ai_floor_plan, ai_interior, ...)
#                + AI Settings.enabled + AI Settings.kill_switch_until
# Rate limit:    per-user per-minute, configured in AI Tool Config
# Free quota:    per-user per-day generation counter for non-premium users
# Budgets:       per-tool daily/monthly + global daily/monthly caps. Auto-trips
#                kill switch when hit.
# Idempotency:   Idempotency-Key header (or body fallback). (user, key) is
#                unique on AI Job; duplicate submits return the original job.
# ============================================================================

from construction.construction.doctype.ai_settings import ai_settings as ai_cfg


_AI_FLAGS = {
    "floor_plan": "ai_floor_plan",
    "interior": "ai_interior",
    "exterior": "ai_exterior",
    "garden": "ai_garden",
    "layout": "ai_layout",
    "cleanup": "ai_cleanup",
    "ref": "ai_ref",
    "paint": "ai_paint",
    "replace": "ai_replace",
    "floor": "ai_floor",
}

# Legacy aliases - clients on older builds may still post "interior_design".
_TOOL_ALIASES = {
    "interior_design": "interior",
}


def _flag_enabled(key):
    enabled = frappe.db.get_value("Feature Flag", key, "enabled")
    return bool(enabled)


def _rate_limit_check(user, tool_id, limit_per_min):
    if not limit_per_min or limit_per_min <= 0:
        return True
    key = f"ai_rl:{tool_id}:{user}"
    cache = frappe.cache()
    current = cache.get_value(key) or 0
    try:
        current = int(current)
    except (TypeError, ValueError):
        current = 0
    if current >= limit_per_min:
        return False
    cache.set_value(key, current + 1, expires_in_sec=60)
    return True


def _free_quota_check(user, tool_id, free_daily_limit):
    """Per-user per-day generation counter for non-premium users.
    Returns (ok: bool, remaining_today: int). Premium bypasses the cap."""
    if not free_daily_limit or free_daily_limit <= 0:
        return True, -1
    if ai_cfg.is_premium(user):
        return True, -1
    from frappe.utils import nowdate
    key = f"ai_free:{user}:{nowdate()}"
    cache = frappe.cache()
    current = cache.get_value(key) or 0
    try:
        current = int(current)
    except (TypeError, ValueError):
        current = 0
    if current >= free_daily_limit:
        return False, 0
    return True, free_daily_limit - current


def _free_quota_increment(user):
    from frappe.utils import nowdate
    key = f"ai_free:{user}:{nowdate()}"
    cache = frappe.cache()
    current = cache.get_value(key) or 0
    try:
        current = int(current)
    except (TypeError, ValueError):
        current = 0
    cache.set_value(key, current + 1, expires_in_sec=60 * 60 * 36)


def _canonical_request_hash(payload):
    canonical = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _spend_in_window(tool_id, since):
    filters = {"completed_at": [">=", since]}
    if tool_id:
        filters["job_type"] = tool_id
    rows = frappe.get_all(
        "AI Job", filters=filters, fields=["cost_cents"], limit_page_length=0
    )
    return sum((r.get("cost_cents") or 0) for r in rows)


def _check_budgets(tool_id, cfg):
    from frappe.utils import add_to_date, now_datetime

    now = now_datetime()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = day_start.replace(day=1)

    job_daily = cfg.get("daily_budget_cents") or 0
    job_monthly = cfg.get("monthly_budget_cents") or 0

    if job_daily:
        spent = _spend_in_window(tool_id, day_start)
        if spent >= job_daily:
            ai_cfg.trip_kill_switch(
                hours=24,
                reason=f"{tool_id} daily budget exceeded ({spent}/{job_daily}c)",
            )
            return _error_response(
                "BUDGET_EXCEEDED",
                "Daily AI budget reached. Try again tomorrow.",
                429,
            )
    if job_monthly:
        spent = _spend_in_window(tool_id, month_start)
        if spent >= job_monthly:
            hours = max(1, int((add_to_date(month_start, months=1) - now).total_seconds() // 3600))
            ai_cfg.trip_kill_switch(
                hours=hours,
                reason=f"{tool_id} monthly budget exceeded ({spent}/{job_monthly}c)",
            )
            return _error_response(
                "BUDGET_EXCEEDED",
                "Monthly AI budget reached. Try again next month.",
                429,
            )

    settings = ai_cfg.get_settings()
    g_daily = settings.global_daily_budget_cents or 0
    g_monthly = settings.global_monthly_budget_cents or 0

    if g_daily:
        spent = _spend_in_window(None, day_start)
        if spent >= g_daily:
            ai_cfg.trip_kill_switch(
                hours=24,
                reason=f"Global daily budget exceeded ({spent}/{g_daily}c)",
            )
            return _error_response("BUDGET_EXCEEDED", "Daily AI budget reached.", 429)
    if g_monthly:
        spent = _spend_in_window(None, month_start)
        if spent >= g_monthly:
            hours = max(1, int((add_to_date(month_start, months=1) - now).total_seconds() // 3600))
            ai_cfg.trip_kill_switch(
                hours=hours,
                reason=f"Global monthly budget exceeded ({spent}/{g_monthly}c)",
            )
            return _error_response("BUDGET_EXCEEDED", "Monthly AI budget reached.", 429)

    return None


def _idempotency_key_from_request(data):
    if frappe.request and frappe.request.headers:
        hdr = frappe.request.headers.get("Idempotency-Key") or frappe.request.headers.get("X-Idempotency-Key")
        if hdr:
            return hdr.strip()
    return data.get("idempotency_key") if isinstance(data, dict) else None


def _save_uploaded_file(field_name):
    """If the request has a multipart file under [field_name], persist it as a
    private File and return its file_url. Returns None when absent, or a
    forwarded error response dict when oversized."""
    if not (frappe.request and frappe.request.files):
        return None
    upload = frappe.request.files.get(field_name)
    if not upload:
        return None
    content = upload.read()
    if len(content) > 10 * 1024 * 1024:
        return _error_response("FILE_TOO_LARGE", "Image must be 10 MB or less", 413)
    from frappe.utils.file_manager import save_file
    file_doc = save_file(
        fname=upload.filename or f"ai_{field_name}.bin",
        content=content,
        dt=None,
        dn=None,
        is_private=1,
    )
    return file_doc.file_url


def _normalize_status(s):
    return "done" if s == "succeeded" else s


def _resolve_tool_id(raw):
    if not raw:
        return None
    return _TOOL_ALIASES.get(raw, raw)


def _ai_submit(tool_id, body, extra_payload):
    """Shared submit pipeline for every AI tool."""
    try:
        _require_auth()
    except frappe.AuthenticationError:
        return _error_response("UNAUTHORIZED", "Authentication required", 401)

    if not ai_cfg.is_master_enabled():
        return _error_response("AI_DISABLED", "AI is currently disabled", 503)

    flag = _AI_FLAGS.get(tool_id)
    if not flag or not _flag_enabled(flag):
        return _error_response(
            "AI_DISABLED",
            f"AI tool {tool_id} is currently unavailable",
            503,
        )

    try:
        cfg = ai_cfg.get_job_config(tool_id)
    except ValueError:
        return _error_response("INVALID_TOOL", f"Unknown AI tool {tool_id}", 400)

    if not cfg.get("enabled"):
        return _error_response("AI_DISABLED", f"Tool {tool_id} disabled", 503)

    user = frappe.session.user
    is_premium = ai_cfg.is_premium(user)

    if cfg.get("premium_only") and not is_premium:
        return _error_response(
            "PREMIUM_REQUIRED",
            "This tool is available to premium users only.",
            402,
        )

    quality = (body.get("quality") or "std").lower()
    if quality == "hd":
        if not is_premium:
            return _error_response("PREMIUM_REQUIRED", "HD output is premium-only.", 402)
        if not cfg.get("hd_supported"):
            return _error_response("HD_NOT_SUPPORTED", "This tool does not support HD output.", 400)

    # Variations: clamp to 1..4, and force free users to a single variation.
    # The client already caps this, but enforce server-side so a tampered
    # client can't pull a 4-up grid (4× the Gemini cost) on the free tier.
    try:
        variations = int(body.get("variations") or 1)
    except (TypeError, ValueError):
        variations = 1
    variations = max(1, min(4, variations))
    if not is_premium:
        variations = 1

    if not _rate_limit_check(user, tool_id, cfg.get("rate_limit_per_min")):
        return _error_response(
            "RATE_LIMITED",
            "Too many requests. Please slow down.",
            429,
        )

    # Single idempotency key (client UUID, from the header) that BOTH the job
    # row and the credit debit dedupe on. Using one key keeps them in lockstep:
    # a re-submit with a fresh key is correctly a brand-new, charged job, while
    # a true retry (same key) is a no-op on both. (Previously the debit keyed on
    # the request hash while the job keyed on the header — so a "Regenerate"
    # with identical settings ran a fresh Gemini job for free.)
    idempotency_key = _idempotency_key_from_request(body)
    if not idempotency_key:
        return _error_response(
            "MISSING_PARAMS",
            "Idempotency-Key header is required (client-generated UUID).",
        )

    # True-retry short-circuit BEFORE any debit, so a replay never even attempts
    # a charge.
    existing = frappe.db.get_value(
        "AI Job",
        {"user": user, "idempotency_key": idempotency_key},
        "name",
    )
    if existing:
        return _ai_status_payload(existing)

    # Budget guard BEFORE the debit so a tripped kill switch can never strand a
    # user's credits on a job that never runs.
    budget_err = _check_budgets(tool_id, cfg)
    if budget_err is not None:
        return budget_err

    # PREM-4: credit ledger first, daily-quota as fallback. When credit gating
    # is off, the legacy free-daily-limit path runs unchanged so nothing
    # breaks before we have IAP wired (PREM-3) and a way for users to top up.
    debit_key = None
    debit_result = None
    if _credit_gating_enabled():
        cost = _credit_cost_for(tool_id, quality, variations)
        if cost > 0:
            debit_key = f"ai_submit:{user}:{idempotency_key}"
            debit_result = _debit_credits(
                user,
                amount=cost,
                ref_doctype="AI Job",
                ref_name="",  # filled in after the doc is inserted
                reason=f"AI submit: {tool_id}",
                idempotency_key=debit_key,
            )
            if debit_result.get("applied", 0) < cost:
                state = _get_credit_state(user)
                return _error_response(
                    "INSUFFICIENT_CREDITS",
                    "Not enough credits for this run. Top up to keep going.",
                    402,
                    details={
                        "cost": cost,
                        "balance": state["total_balance"],
                        "monthly_balance": state["monthly_balance"],
                        "topup_balance": state["topup_balance"],
                    },
                )
    else:
        ok, remaining = _free_quota_check(user, tool_id, cfg.get("free_daily_limit"))
        if not ok:
            return _error_response(
                "FREE_QUOTA_EXCEEDED",
                "Daily free AI quota reached. Upgrade to premium for unlimited generations.",
                429,
                details={"remaining_today": 0},
            )

    payload = {k: v for k, v in body.items() if k != "idempotency_key"}
    payload.update(extra_payload or {})
    payload["variations"] = variations  # persist the clamped value

    try:
        doc = frappe.new_doc("AI Job")
        doc.job_type = tool_id
        doc.status = "queued"
        doc.user = user
        doc.idempotency_key = idempotency_key
        doc.request_hash = _canonical_request_hash(payload)
        doc.request_payload_json = json.dumps(payload)
        doc.quality = quality
        doc.variation_count = variations
        doc.source_image_url = extra_payload.get("image_url") if extra_payload else None
        doc.mask_url = extra_payload.get("mask_url") if extra_payload else None
        doc.ref_image_url = extra_payload.get("ref_image_url") if extra_payload else None
        doc.insert(ignore_permissions=True)

        # PREM-4: now that the job has a name, point the credit-debit rows at
        # it so the worker can issue a refund-by-ref if the job fails. Scoped to
        # THIS submit's debit key so we never touch another job's ledger rows.
        if debit_key:
            try:
                frappe.db.sql(
                    """update `tabAI Credit Ledger`
                       set ref_name = %(name)s
                       where user = %(user)s
                         and ref_doctype = 'AI Job'
                         and (ref_name is null or ref_name = '')
                         and idempotency_key like %(key_prefix)s""",
                    {
                        "name": doc.name,
                        "user": user,
                        "key_prefix": f"{debit_key}%",
                    },
                )
            except Exception:
                # Backfill is opportunistic — refund still works via job name
                # if the ledger insert raced, just slower (scan by user).
                pass

        frappe.db.commit()

        if not _credit_gating_enabled():
            _free_quota_increment(user)

        frappe.enqueue(
            "construction.api.ai_worker.run_job",
            queue="long",
            timeout=600,
            name=doc.name,
        )

        return _ai_status_payload(doc.name)
    except Exception:
        corr = _log_unhandled(f"v1.ai_submit:{tool_id}")
        # Compensate the debit so a crash before the job is safely enqueued can
        # never leave the user charged for nothing.
        if debit_result and debit_result.get("applied", 0) > 0:
            try:
                _reverse_debit(user, debit_key, debit_result)
                frappe.db.commit()
            except Exception:
                frappe.log_error(frappe.get_traceback(), "v1.ai_submit.reverse_failed")
        return _error_response(
            "SERVER_ERROR", "Failed to enqueue AI job", 500, correlation_id=corr
        )


def _ai_status_payload(name):
    doc = frappe.get_doc("AI Job", name)
    if doc.user != frappe.session.user:
        return _error_response("FORBIDDEN", "You do not own this job", 403)
    return {
        "job_id": doc.name,
        "job_type": doc.job_type,
        "status": _normalize_status(doc.status),
        "result": json.loads(doc.result_json or "null"),
        "error": (
            {"code": doc.error_code, "message": doc.error_message}
            if doc.error_code
            else None
        ),
        "cost_cents": doc.cost_cents or 0,
        "quality": doc.quality or "std",
        "variation_count": doc.variation_count or 1,
        "started_at": str(doc.started_at) if doc.started_at else None,
        "completed_at": str(doc.completed_at) if doc.completed_at else None,
    }


@frappe.whitelist(methods=["POST"])
def ai_generate(**kwargs):
    """Unified AI generation endpoint for all 9 design tools.

    Multipart body:
        tool_id      interior | exterior | garden | layout | cleanup | ref | paint | replace | floor
        image        <file>         required for all 9
        style        <slug>         required
        room         <slug>         required iff tool_id == interior
        color        <slug>         optional
        notes        <free text>    optional
        quality      std | hd       default std; hd is premium-only
        variations   1..4           default 1; clamped to 4
        mask         <file>         optional (cleanup, replace)
        ref_image    <file>         optional (ref tool)

    Header: Idempotency-Key: <uuid>
    """
    form = frappe.request.form if (frappe.request and frappe.request.form) else {}
    body = dict(form)
    tool_id = _resolve_tool_id(body.get("tool_id"))
    if not tool_id or tool_id not in _AI_FLAGS:
        return _error_response("INVALID_TOOL", "Missing or unknown tool_id", 400)
    if tool_id == "floor_plan":
        return _error_response(
            "INVALID_TOOL",
            "Use ai_floor_plan_analyze for floor plan analysis.",
            400,
        )

    image_url = _save_uploaded_file("image")
    if isinstance(image_url, dict) and image_url.get("ok") is False:
        return image_url
    if not image_url:
        return _error_response("MISSING_IMAGE", "A source image is required.", 400)

    extras = {"image_url": image_url}
    mask_url = _save_uploaded_file("mask")
    if isinstance(mask_url, dict) and mask_url.get("ok") is False:
        return mask_url
    if mask_url:
        extras["mask_url"] = mask_url

    ref_url = _save_uploaded_file("ref_image")
    if isinstance(ref_url, dict) and ref_url.get("ok") is False:
        return ref_url
    if ref_url:
        extras["ref_image_url"] = ref_url

    return _ai_submit(tool_id, body, extras)


@frappe.whitelist(methods=["POST"])
def ai_floor_plan_analyze(**kwargs):
    """Submit a floor-plan image for AI analysis (vision JSON, not image edit).

    Multipart fields: image=<file>, city=<str>, quality_level=<str>
    Header: Idempotency-Key: <uuid>
    """
    image_url = _save_uploaded_file("image")
    if isinstance(image_url, dict) and image_url.get("ok") is False:
        return image_url

    extras = {}
    if image_url:
        extras["image_url"] = image_url
    if frappe.request and frappe.request.form:
        for key in ("city", "country", "quality_level"):
            val = frappe.request.form.get(key)
            if val:
                extras[key] = val

    body = dict(frappe.request.form) if (frappe.request and frappe.request.form) else {}
    return _ai_submit("floor_plan", body, extras)


@frappe.whitelist(methods=["POST"])
def ai_interior_generate(**kwargs):
    """Legacy interior endpoint - accepts JSON body without a source image.

    Kept for the legacy interior_design_page in the Flutter app. New clients
    should use /ai_generate with tool_id=interior and a source image.
    """
    body = _read_json_body() or {}
    return _ai_submit("interior", body, {})


@frappe.whitelist()
def job_status(id=None, job_id=None):
    """Unified poll endpoint. Accepts either ?id=<> or ?job_id=<>."""
    try:
        _require_auth()
    except frappe.AuthenticationError:
        return _error_response("UNAUTHORIZED", "Authentication required", 401)
    name = id or job_id
    if not name:
        return _error_response("MISSING_PARAMS", "id is required")
    if not frappe.db.exists("AI Job", name):
        return _error_response("NOT_FOUND", "Job not found", 404)
    return _ai_status_payload(name)


# Backwards-compat aliases
ai_floor_plan_submit = ai_floor_plan_analyze
ai_interior_submit = ai_interior_generate


@frappe.whitelist()
def ai_floor_plan_status(job_id=None):
    return job_status(job_id=job_id)


@frappe.whitelist()
def ai_interior_status(job_id=None):
    return job_status(job_id=job_id)


# ============================================================================
# AI Design persistence (PR-BE-4)
# ============================================================================


@frappe.whitelist(methods=["POST"])
def ai_save_design(**kwargs):
    """Promote a finished AI Job result into a permanent AI Design row.

    Body (JSON or form):
        job_id           required, must be owned by caller
        variation_index  0..3, default 0
        style, room, color, notes - copied from request if not on the job
    """
    try:
        _require_auth()
    except frappe.AuthenticationError:
        return _error_response("UNAUTHORIZED", "Authentication required", 401)

    body = _read_json_body() or {}
    if not body and frappe.request and frappe.request.form:
        body = dict(frappe.request.form)
    job_id = body.get("job_id")
    if not job_id:
        return _error_response("MISSING_PARAMS", "job_id is required", 400)

    if not frappe.db.exists("AI Job", job_id):
        return _error_response("NOT_FOUND", "Job not found", 404)

    job = frappe.get_doc("AI Job", job_id)
    if job.user != frappe.session.user:
        return _error_response("FORBIDDEN", "You do not own this job", 403)
    if job.status != "succeeded":
        return _error_response("NOT_READY", "Job has not finished successfully", 409)

    result = json.loads(job.result_json or "null") or {}
    images = result.get("images") or []
    try:
        idx = int(body.get("variation_index") or 0)
    except (TypeError, ValueError):
        idx = 0
    if idx < 0 or idx >= len(images):
        return _error_response("BAD_VARIATION", "variation_index out of range", 400)

    sel = images[idx] if isinstance(images[idx], dict) else {}
    result_url = sel.get("url")
    if not result_url:
        return _error_response("NO_RESULT", "Selected variation has no image URL", 422)

    req_payload = json.loads(job.request_payload_json or "{}")

    design = frappe.new_doc("AI Design")
    design.user = frappe.session.user
    design.tool_id = job.job_type
    design.job_id = job.name
    design.style = body.get("style") or req_payload.get("style")
    design.room = body.get("room") or req_payload.get("room")
    design.color = body.get("color") or req_payload.get("color")
    design.notes = body.get("notes") or req_payload.get("notes")
    design.source_file = job.source_image_url
    design.result_file = result_url
    design.thumb_file = sel.get("thumb_url")
    design.params_json = json.dumps({**req_payload, "variation_index": idx})
    design.model = job.model
    design.seed = str(sel.get("seed")) if sel.get("seed") is not None else None
    design.insert(ignore_permissions=True)
    frappe.db.commit()
    return {
        "id": design.name,
        "result_file": design.result_file,
        "source_file": design.source_file,
        "tool_id": design.tool_id,
        "style": design.style,
        "room": design.room,
        "color": design.color,
        "created_at": str(design.creation),
    }


@frappe.whitelist()
def list_ai_designs(limit=50, offset=0, tool_id=None):
    """List saved designs for the authenticated user, newest first."""
    try:
        _require_auth()
    except frappe.AuthenticationError:
        return _error_response("UNAUTHORIZED", "Authentication required", 401)
    filters = {"user": frappe.session.user}
    if tool_id:
        filters["tool_id"] = tool_id
    try:
        limit = max(1, min(200, int(limit)))
        offset = max(0, int(offset))
    except (TypeError, ValueError):
        limit, offset = 50, 0
    rows = frappe.get_all(
        "AI Design",
        filters=filters,
        fields=["name", "tool_id", "style", "room", "color", "source_file",
                "result_file", "thumb_file", "model", "creation"],
        order_by="creation desc",
        limit_start=offset,
        limit_page_length=limit,
    )
    return {"items": rows, "count": len(rows)}


@frappe.whitelist(methods=["POST"])
def delete_ai_design(id=None, **kwargs):
    try:
        _require_auth()
    except frappe.AuthenticationError:
        return _error_response("UNAUTHORIZED", "Authentication required", 401)
    if not id:
        body = _read_json_body() or {}
        id = body.get("id")
    if not id:
        return _error_response("MISSING_PARAMS", "id is required", 400)
    if not frappe.db.exists("AI Design", id):
        return _error_response("NOT_FOUND", "Design not found", 404)
    design = frappe.get_doc("AI Design", id)
    if design.user != frappe.session.user:
        return _error_response("FORBIDDEN", "You do not own this design", 403)
    frappe.delete_doc("AI Design", id, ignore_permissions=True)
    frappe.db.commit()
    return {"deleted": id}


@frappe.whitelist()
def ai_quota_status():
    """Returns the caller's remaining free generations today + premium status."""
    try:
        _require_auth()
    except frappe.AuthenticationError:
        return _error_response("UNAUTHORIZED", "Authentication required", 401)
    from frappe.utils import nowdate
    user = frappe.session.user
    premium = ai_cfg.is_premium(user)
    if premium:
        return {"premium": True, "remaining_today": -1}
    key = f"ai_free:{user}:{nowdate()}"
    used = int(frappe.cache().get_value(key) or 0)
    cfg = ai_cfg.get_job_config("interior")
    free_limit = int(cfg.get("free_daily_limit") or 0)
    remaining = max(0, free_limit - used) if free_limit else -1
    return {"premium": False, "remaining_today": remaining, "free_daily_limit": free_limit}


# ============================================================================
# Premium subscription (real 7-day free trial; IAP receipt validation hook)
# ============================================================================


@frappe.whitelist()
def premium_status():
    """Returns current premium state for the caller."""
    try:
        _require_auth()
    except frappe.AuthenticationError:
        return _error_response("UNAUTHORIZED", "Authentication required", 401)
    user = frappe.session.user
    is_premium = ai_cfg.is_premium(user)
    expires_at = None
    source = None
    trial_used = False
    settings = ai_cfg.get_settings()
    for row in (settings.get("premium_users") or []):
        if row.user != user:
            continue
        if (row.note or "") == "trial":
            trial_used = True
        if row.expires_at and (expires_at is None or row.expires_at > expires_at):
            expires_at = row.expires_at
            source = row.note or "manual"
    return {
        "premium": is_premium,
        "expires_at": str(expires_at) if expires_at else None,
        "source": source,
        "trial_used": trial_used,
    }


@frappe.whitelist(methods=["POST"])
def start_premium_trial(**kwargs):
    """Grant the caller a 7-day premium trial. Idempotent — re-calling while
    a trial is already active returns the existing entry; calling after a
    prior trial expired returns TRIAL_USED."""
    try:
        _require_auth()
    except frappe.AuthenticationError:
        return _error_response("UNAUTHORIZED", "Authentication required", 401)
    from datetime import timedelta
    from frappe.utils import now_datetime
    user = frappe.session.user
    settings = frappe.get_doc("AI Settings")
    now = now_datetime()
    for row in (settings.get("premium_users") or []):
        if row.user != user:
            continue
        if (row.note or "") != "trial":
            continue
        if not row.expires_at or row.expires_at > now:
            return {
                "premium": True,
                "expires_at": str(row.expires_at) if row.expires_at else None,
                "source": "trial",
                "trial_used": True,
                "already_active": True,
            }
        return _error_response(
            "TRIAL_USED",
            "You've already used your 7-day trial. Subscribe to keep premium features.",
            409,
        )
    expires = now + timedelta(days=7)
    settings.append("premium_users", {
        "user": user,
        "expires_at": expires,
        "note": "trial",
    })
    settings.save(ignore_permissions=True)
    frappe.db.commit()
    return {
        "premium": True,
        "expires_at": str(expires),
        "source": "trial",
        "trial_used": True,
        "already_active": False,
    }


@frappe.whitelist(methods=["POST"])
def activate_premium(**kwargs):
    """Activate or extend premium for the caller from a purchased receipt.

    Body (JSON):
        platform: "android" | "ios"
        product_id: e.g. "buildcost_premium_monthly" | "buildcost_premium_annual"
        receipt:    raw receipt string from Google Play / App Store
        expires_at: ISO8601 datetime (client estimate; server is authoritative
                    once real receipt validation is wired)

    For v1 we trust the client's expiry — production-deploy step is to add a
    Google Play Developer API / App Store Server API verify call here, then
    overwrite expires_at with the verified value.
    """
    try:
        _require_auth()
    except frappe.AuthenticationError:
        return _error_response("UNAUTHORIZED", "Authentication required", 401)
    body = _read_json_body() or {}
    expires_raw = body.get("expires_at")
    product_id = body.get("product_id") or "premium"
    if not expires_raw:
        return _error_response("MISSING_PARAMS", "expires_at is required", 400)
    from frappe.utils import get_datetime, now_datetime
    try:
        expires = get_datetime(expires_raw)
    except Exception:
        return _error_response("INVALID_DATE", "expires_at must be ISO8601", 400)
    if expires <= now_datetime():
        return _error_response("EXPIRED", "expires_at is in the past", 400)

    user = frappe.session.user
    settings = frappe.get_doc("AI Settings")
    found = False
    for row in (settings.get("premium_users") or []):
        if row.user == user and (row.note or "") == product_id:
            row.expires_at = expires
            found = True
            break
    if not found:
        settings.append("premium_users", {
            "user": user,
            "expires_at": expires,
            "note": product_id,
        })
    settings.save(ignore_permissions=True)
    frappe.db.commit()
    return {
        "premium": True,
        "expires_at": str(expires),
        "source": product_id,
    }


# ---------------------------------------------------------------------------
# POST /v1/log_client_error  - record a client-side error report
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=True, methods=["POST"])
def log_client_error(**kwargs):
    """Record a client-side error report in App Error Log.

    Body (JSON) \u2014 all optional except message:
      message, stack, error_type, source (app|api), level, is_fatal,
      route, endpoint, http_method, http_status, request_id, correlation_id,
      app_version, build_number, platform, os_version, device_model, device_id,
      network_status, environment, breadcrumbs, context_json.

    Returns {ok, correlation_id} so the client can surface the id.
    """
    try:
        data = _read_json_body() if not kwargs else kwargs
        if not isinstance(data, dict):
            data = {}

        correlation_id = (str(data.get("correlation_id") or "")[:64]
                          or uuid.uuid4().hex[:12])

        from construction.construction.doctype.app_error_log.app_error_log import (
            AppErrorLog,
        )
        source = data.get("source") or "app"
        AppErrorLog.record(
            source=source if source in ("app", "api") else "app",
            level=data.get("level"),
            is_fatal=bool(data.get("is_fatal")),
            title=data.get("title"),
            message=data.get("message") or data.get("stack") or "Unknown client error",
            error_type=data.get("error_type"),
            # Accept both `stack` (legacy) and `stack_trace`.
            stack_trace=data.get("stack_trace") or data.get("stack"),
            endpoint=data.get("endpoint"),
            http_method=data.get("http_method"),
            http_status=data.get("http_status"),
            request_id=data.get("request_id"),
            route=data.get("route"),
            correlation_id=correlation_id,
            network_status=data.get("network_status"),
            breadcrumbs=data.get("breadcrumbs"),
            context_json=data.get("context_json"),
            device_model=data.get("device_model"),
            os_version=data.get("os_version"),
            device_id=data.get("device_id"),
            app_version=data.get("app_version"),
            build_number=data.get("build_number"),
            platform=data.get("platform"),
            environment=data.get("environment"),
            user=(frappe.session.user if frappe.session.user != "Guest" else None),
        )
        frappe.db.commit()
        return {"ok": True, "correlation_id": correlation_id}
    except Exception:
        corr = _log_unhandled("v1.log_client_error")
        return _error_response("SERVER_ERROR", "Could not record report", 500, correlation_id=corr)


# ---------------------------------------------------------------------------
# POST /v1/sync_expenses  - two-way sync for expense projects + entries
# ---------------------------------------------------------------------------

@frappe.whitelist(methods=["POST"])
def sync_expenses(**kwargs):
    """Push the client's expense projects + entries; receive the authoritative
    server list back.

    Body shape:
      {
        "projects": [
          {"client_id", "name", "location", "budget", "estimate_id"?,
           "created_at", "updated_at", "deleted"},
          ...
        ],
        "expenses": [
          {"client_id", "project_client_id", "category", "custom_name"?,
           "material", "qty"?, "uom"?, "amount", "note"?, "date"?,
           "created_at", "updated_at", "deleted"},
          ...
        ]
      }

    Conflict resolution is last-write-wins via `updated_at` — a payload row is
    only applied when its `updated_at` is strictly newer than the server's
    (or the server row does not yet exist). Tombstones (`deleted=1`) are kept
    so deletions propagate to other devices on the same account.

    Idempotent on `(user, client_id)`. Per-row failures are reported back in
    `failed` and do not abort the batch.
    """
    try:
        _require_auth()
    except frappe.AuthenticationError:
        return _error_response("UNAUTHORIZED", "Authentication required", 401)

    data = _read_json_body()
    if data is None:
        return _error_response("INVALID_JSON", "Body is not valid JSON")

    incoming_projects = data.get("projects") or []
    incoming_expenses = data.get("expenses") or []
    if not isinstance(incoming_projects, list) or not isinstance(incoming_expenses, list):
        return _error_response(
            "INVALID_PARAMS",
            "'projects' and 'expenses' must each be a list",
        )

    user = frappe.session.user
    saved_projects, failed_projects = _upsert_many(
        "Expense Project", user, incoming_projects, _apply_expense_project
    )
    saved_expenses, failed_expenses = _upsert_many(
        "Expense Entry", user, incoming_expenses, _apply_expense_entry
    )

    frappe.db.commit()

    server_projects = frappe.get_all(
        "Expense Project",
        filters={"user": user},
        fields=[
            "client_id", "project_name", "location", "budget",
            "estimate_client_id", "client_created_at", "updated_at", "deleted",
        ],
        order_by="updated_at desc",
        limit_page_length=0,
    )
    server_expenses = frappe.get_all(
        "Expense Entry",
        filters={"user": user},
        fields=[
            "client_id", "project_client_id", "category", "custom_name",
            "material", "qty", "uom", "amount", "note", "entry_date",
            "client_created_at", "updated_at", "deleted",
        ],
        order_by="updated_at desc",
        limit_page_length=0,
    )

    for r in server_projects:
        r["created_at"] = str(r.pop("client_created_at")) if r.get("client_created_at") else None
        r["updated_at"] = str(r["updated_at"]) if r.get("updated_at") else None
        r["name"] = r.pop("project_name") or ""
        r["estimate_id"] = r.pop("estimate_client_id") or None
        r["deleted"] = bool(r.get("deleted"))

    for r in server_expenses:
        r["created_at"] = str(r.pop("client_created_at")) if r.get("client_created_at") else None
        r["updated_at"] = str(r["updated_at"]) if r.get("updated_at") else None
        r["date"] = str(r.pop("entry_date")) if r.get("entry_date") else None
        r["deleted"] = bool(r.get("deleted"))

    return {
        "ok": True,
        "projects": server_projects,
        "expenses": server_expenses,
        "saved_projects": saved_projects,
        "failed_projects": failed_projects,
        "saved_expenses": saved_expenses,
        "failed_expenses": failed_expenses,
    }


def _upsert_many(doctype, user, rows, apply_fn):
    saved, failed = [], []
    for row in rows:
        if not isinstance(row, dict) or not row.get("client_id"):
            failed.append({"reason": "missing client_id"})
            continue
        client_id = row["client_id"]
        try:
            existing_name = frappe.db.get_value(
                doctype, {"user": user, "client_id": client_id}, "name"
            )
            incoming_updated = _parse_dt(row.get("updated_at"))
            if existing_name:
                server_updated = frappe.db.get_value(doctype, existing_name, "updated_at")
                if (
                    server_updated
                    and incoming_updated
                    and incoming_updated <= server_updated
                ):
                    # Server is newer — keep it.
                    continue
                doc = frappe.get_doc(doctype, existing_name)
            else:
                doc = frappe.new_doc(doctype)
                doc.client_id = client_id
                doc.user = user
            apply_fn(doc, row)
            doc.save(ignore_permissions=True)
            saved.append(client_id)
        except Exception:
            corr = _log_unhandled(f"v1.sync_expenses.{doctype.lower().replace(' ', '_')}")
            failed.append({
                "client_id": client_id,
                "reason": "server_error",
                "correlation_id": corr,
            })
    return saved, failed


def _apply_expense_project(doc, row):
    doc.project_name = (row.get("name") or "")[:160]
    doc.location = (row.get("location") or "")[:160]
    doc.budget = float(row.get("budget") or 0)
    doc.estimate_client_id = row.get("estimate_id") or ""
    doc.client_created_at = (
        _parse_dt(row.get("created_at")) or frappe.utils.now_datetime()
    )
    doc.updated_at = (
        _parse_dt(row.get("updated_at")) or frappe.utils.now_datetime()
    )
    doc.deleted = 1 if row.get("deleted") else 0


def _apply_expense_entry(doc, row):
    doc.project_client_id = (row.get("project_client_id") or "")[:140]
    doc.category = (row.get("category") or "")[:60]
    doc.custom_name = (row.get("custom_name") or "")[:160]
    doc.material = (row.get("material") or "")[:160]
    doc.qty = float(row.get("qty")) if row.get("qty") is not None else None
    doc.uom = (row.get("uom") or "")[:40]
    doc.amount = float(row.get("amount") or 0)
    doc.note = (row.get("note") or "")[:1000]
    doc.entry_date = _parse_dt(row.get("date"))
    doc.client_created_at = (
        _parse_dt(row.get("created_at")) or frappe.utils.now_datetime()
    )
    doc.updated_at = (
        _parse_dt(row.get("updated_at")) or frappe.utils.now_datetime()
    )
    doc.deleted = 1 if row.get("deleted") else 0


def _parse_dt(value):
    """Tolerant ISO-8601 / date / datetime parser. Returns a datetime or None."""
    if not value:
        return None
    if hasattr(value, "year"):
        return value
    try:
        return frappe.utils.get_datetime(str(value))
    except Exception:
        try:
            return frappe.utils.getdate(str(value))
        except Exception:
            return None


# ===========================================================================
# AI CREDIT LEDGER (PREM-1)
# ===========================================================================
# Append-only ledger of credit movements. Balance is a projection over deltas
# scoped to the current monthly period for the `monthly` bucket and lifetime
# for the `topup` bucket. Debits prefer monthly first so users don't burn
# top-ups they paid for while their monthly quota goes unused.
#
# Idempotency: every event carries an optional `idempotency_key` (unique). Use
# it for RTDN webhooks and IAP receipts so retries don't double-apply.
# ===========================================================================

# Default per-month grant for active Pro subscribers. Per
# docs/PREMIUM_MONETIZATION_PLAN.md the locked decision is 80, no rollover.
_DEFAULT_MONTHLY_QUOTA = 80
# Welcome grant given once per device-identity (anti-abuse anchored on SSAID +
# Play Integrity, enforced higher up; this just sets the amount).
_DEFAULT_WELCOME_QUOTA = 5


def _current_period_month():
    """Return the current billing month as 'YYYY-MM'."""
    now = frappe.utils.now_datetime()
    return now.strftime("%Y-%m")


def _next_period_start(period_month=None):
    """First instant of the month after `period_month` (or after the current
    period when not specified). Used for `next_reset_at` in the API response."""
    period = period_month or _current_period_month()
    year, month = (int(x) for x in period.split("-"))
    if month == 12:
        return frappe.utils.get_datetime(f"{year + 1}-01-01 00:00:00")
    return frappe.utils.get_datetime(f"{year}-{month + 1:02d}-01 00:00:00")


def _sum_deltas(user, bucket, period_month=None):
    """Sum of deltas for the user/bucket. When `period_month` is given, only
    events tagged with that period count (used for the monthly bucket).
    """
    filters = {"user": user, "bucket": bucket}
    if period_month:
        # Monthly grants are tagged; debits/refunds against monthly inherit the
        # tag from the originating period so a fresh month starts clean.
        filters["period_month"] = period_month
    rows = frappe.get_all(
        "AI Credit Ledger",
        filters=filters,
        fields=["delta"],
        limit_page_length=0,
    )
    return sum(int(r.delta or 0) for r in rows)


def _get_credit_state(user, monthly_quota=_DEFAULT_MONTHLY_QUOTA):
    """Computed credit state for `user`. Read-only; never mutates."""
    period = _current_period_month()
    monthly_balance = max(0, _sum_deltas(user, "monthly", period_month=period))
    topup_balance = max(0, _sum_deltas(user, "topup"))
    monthly_used = max(0, monthly_quota - monthly_balance)
    return {
        "monthly_balance": monthly_balance,
        "monthly_quota": monthly_quota,
        "monthly_used": monthly_used,
        "topup_balance": topup_balance,
        "total_balance": monthly_balance + topup_balance,
        "period_month": period,
        "next_reset_at": str(_next_period_start(period)),
    }


def _insert_ledger_event(
    user,
    event_type,
    bucket,
    delta,
    reason=None,
    ref_doctype=None,
    ref_name=None,
    idempotency_key=None,
    period_month=None,
):
    """Insert a single ledger row, returning the snapshot balance afterwards.

    When `idempotency_key` is set and a row already exists with that key, this
    is a no-op (returns the existing row's balance_after). Callers can rely on
    this for webhook retries.
    """
    if idempotency_key:
        existing = frappe.db.get_value(
            "AI Credit Ledger",
            {"idempotency_key": idempotency_key},
            ["balance_after"],
            as_dict=True,
        )
        if existing:
            return int(existing.get("balance_after") or 0)

    # Compute new total balance for the snapshot. Must include this delta.
    snapshot = _get_credit_state(user)
    if bucket == "monthly":
        snapshot_total = max(0, snapshot["monthly_balance"] + delta) + snapshot["topup_balance"]
    else:
        snapshot_total = snapshot["monthly_balance"] + max(0, snapshot["topup_balance"] + delta)

    doc = frappe.new_doc("AI Credit Ledger")
    doc.user = user
    doc.event_type = event_type
    doc.bucket = bucket
    doc.delta = int(delta)
    doc.balance_after = snapshot_total
    doc.reason = (reason or "")[:200]
    doc.ref_doctype = (ref_doctype or "")[:60]
    doc.ref_name = (ref_name or "")[:200]
    doc.idempotency_key = idempotency_key or ""
    doc.period_month = period_month or ""
    doc.insert(ignore_permissions=True)
    return snapshot_total


def _grant_monthly_credits(user, period_month=None, quota=_DEFAULT_MONTHLY_QUOTA, reason=None):
    """Idempotent on (user, period_month). Resets monthly bucket to `quota`.

    No-rollover model: instead of accumulating, we reset by emitting a single
    grant for the target period. Re-running this for the same period is a
    no-op thanks to the idempotency key.
    """
    period = period_month or _current_period_month()
    idem = f"grant_monthly:{user}:{period}"
    return _insert_ledger_event(
        user,
        event_type="grant_monthly",
        bucket="monthly",
        delta=int(quota),
        reason=reason or f"Monthly Pro grant for {period}",
        idempotency_key=idem,
        period_month=period,
    )


def _grant_welcome_credits(user, amount=_DEFAULT_WELCOME_QUOTA, idempotency_key=None, reason=None):
    """Idempotent on `idempotency_key` — caller should pass a hashed device id
    + 'welcome' so each device only ever gets the grant once.
    """
    return _insert_ledger_event(
        user,
        event_type="grant_welcome",
        bucket="topup",
        delta=int(amount),
        reason=reason or "Welcome design credits",
        idempotency_key=idempotency_key,
    )


# Per-IP daily ceiling on NEW welcome grants. This is a *secondary* speed bump
# against credit farming via rotating device_ids — NOT a real defense. The real
# fix is Play Integrity attestation on the device_id at bootstrap (needs client
# work); the hard money backstop is the global budget kill switch in
# `_check_budgets`. Kept generous because this is a mobile app: carrier-grade
# NAT can route many legitimate new installs through one IP, so a tight cap
# would deny real users their welcome credits. Fail-open everywhere.
_WELCOME_IP_DAILY_CAP = 50


def _welcome_ip_allowed():
    """True if this client IP hasn't exceeded the daily new-welcome-grant cap.
    Fail-open: any cache/lookup error returns True so we never block a real
    user's onboarding over a counter."""
    try:
        ip = getattr(frappe.local, "request_ip", None)
        if not ip and frappe.request:
            fwd = frappe.request.headers.get("X-Forwarded-For", "")
            ip = fwd.split(",")[0].strip() if fwd else None
        if not ip:
            return True
        from frappe.utils import nowdate
        key = f"welcome_ip:{ip}:{nowdate()}"
        cache = frappe.cache()
        n = cache.get_value(key) or 0
        try:
            n = int(n)
        except (TypeError, ValueError):
            n = 0
        if n >= _WELCOME_IP_DAILY_CAP:
            return False
        cache.set_value(key, n + 1, expires_in_sec=60 * 60 * 36)
        return True
    except Exception:
        return True


def _grant_topup_credits(user, amount, idempotency_key, reason=None, ref_name=None):
    """Idempotent on the receipt-derived `idempotency_key`. Adds to the topup
    bucket which never expires.
    """
    return _insert_ledger_event(
        user,
        event_type="grant_topup",
        bucket="topup",
        delta=int(amount),
        reason=reason or "Top-up pack purchase",
        ref_doctype="Top-up Receipt",
        ref_name=ref_name,
        idempotency_key=idempotency_key,
    )


def _debit_credits(user, amount, ref_doctype, ref_name, reason=None, idempotency_key=None):
    """Atomically debit `amount` credits from the user, monthly first then
    topup. Splits across buckets when monthly is short.

    Returns a dict {applied: int, monthly_taken: int, topup_taken: int,
    balance_after: int}. If the user has < amount credits in total, applies
    nothing and returns applied=0 — caller is responsible for surfacing the
    out-of-credits error.

    Idempotency: when `idempotency_key` is set, a re-debit with the same key
    is a no-op (returns the same applied count derived from existing rows).
    """
    amount = int(amount)
    if amount <= 0:
        return {"applied": 0, "monthly_taken": 0, "topup_taken": 0, "balance_after": 0}

    # Idempotency check: look up any prior debit with this key.
    if idempotency_key:
        prior = frappe.get_all(
            "AI Credit Ledger",
            filters={"idempotency_key": ["like", f"{idempotency_key}%"]},
            fields=["delta", "bucket", "balance_after"],
        )
        if prior:
            monthly_taken = sum(-int(r.delta) for r in prior if r.bucket == "monthly" and int(r.delta) < 0)
            topup_taken = sum(-int(r.delta) for r in prior if r.bucket == "topup" and int(r.delta) < 0)
            return {
                "applied": monthly_taken + topup_taken,
                "monthly_taken": monthly_taken,
                "topup_taken": topup_taken,
                "balance_after": int(prior[0].balance_after or 0),
            }

    state = _get_credit_state(user)
    if state["total_balance"] < amount:
        return {"applied": 0, "monthly_taken": 0, "topup_taken": 0, "balance_after": state["total_balance"]}

    monthly_take = min(state["monthly_balance"], amount)
    topup_take = amount - monthly_take
    balance_after = 0

    if monthly_take > 0:
        balance_after = _insert_ledger_event(
            user,
            event_type="debit_submit",
            bucket="monthly",
            delta=-monthly_take,
            reason=reason or "AI submission",
            ref_doctype=ref_doctype,
            ref_name=ref_name,
            idempotency_key=(f"{idempotency_key}:monthly" if idempotency_key else None),
            period_month=state["period_month"],
        )
    if topup_take > 0:
        balance_after = _insert_ledger_event(
            user,
            event_type="debit_submit",
            bucket="topup",
            delta=-topup_take,
            reason=reason or "AI submission",
            ref_doctype=ref_doctype,
            ref_name=ref_name,
            idempotency_key=(f"{idempotency_key}:topup" if idempotency_key else None),
        )

    return {
        "applied": monthly_take + topup_take,
        "monthly_taken": monthly_take,
        "topup_taken": topup_take,
        "balance_after": balance_after,
    }


def _refund_credits(user, ref_doctype, ref_name, reason=None):
    """Mirror a prior debit by `ref_doctype` + `ref_name`. Sums the negative
    deltas on those rows and emits matching positive events into the same
    buckets so the refund lands back where it came from.

    Idempotent via a derived key (`refund:<ref_doctype>:<ref_name>`).
    """
    debits = frappe.get_all(
        "AI Credit Ledger",
        filters={
            "user": user,
            "ref_doctype": ref_doctype,
            "ref_name": ref_name,
            "event_type": "debit_submit",
        },
        fields=["delta", "bucket"],
    )
    if not debits:
        return {"applied": 0, "balance_after": _get_credit_state(user)["total_balance"]}

    monthly_amt = -sum(int(r.delta) for r in debits if r.bucket == "monthly" and int(r.delta) < 0)
    topup_amt = -sum(int(r.delta) for r in debits if r.bucket == "topup" and int(r.delta) < 0)
    base_key = f"refund:{ref_doctype}:{ref_name}"
    balance_after = 0

    if monthly_amt > 0:
        balance_after = _insert_ledger_event(
            user,
            event_type="refund_failure",
            bucket="monthly",
            delta=monthly_amt,
            reason=reason or "Refund on job failure",
            ref_doctype=ref_doctype,
            ref_name=ref_name,
            idempotency_key=f"{base_key}:monthly",
            period_month=_current_period_month(),
        )
    if topup_amt > 0:
        balance_after = _insert_ledger_event(
            user,
            event_type="refund_failure",
            bucket="topup",
            delta=topup_amt,
            reason=reason or "Refund on job failure",
            ref_doctype=ref_doctype,
            ref_name=ref_name,
            idempotency_key=f"{base_key}:topup",
        )

    return {"applied": monthly_amt + topup_amt, "balance_after": balance_after}


def _reverse_debit(user, debit_key, debit_result):
    """Compensating reversal for a debit that succeeded but whose job failed to
    enqueue (so the ref_name backfill never ran and `_refund_credits`, which
    keys on ref, can't find it). Mirrors the per-bucket amounts back into the
    ledger. Idempotent on a derived reversal key.
    """
    if not debit_result:
        return
    monthly_taken = int(debit_result.get("monthly_taken", 0) or 0)
    topup_taken = int(debit_result.get("topup_taken", 0) or 0)
    if monthly_taken > 0:
        _insert_ledger_event(
            user,
            event_type="refund_failure",
            bucket="monthly",
            delta=monthly_taken,
            reason="Reversed: submit crashed before enqueue",
            ref_doctype="AI Job",
            ref_name="",
            idempotency_key=f"{debit_key}:reversal:monthly",
            period_month=_current_period_month(),
        )
    if topup_taken > 0:
        _insert_ledger_event(
            user,
            event_type="refund_failure",
            bucket="topup",
            delta=topup_taken,
            reason="Reversed: submit crashed before enqueue",
            ref_doctype="AI Job",
            ref_name="",
            idempotency_key=f"{debit_key}:reversal:topup",
        )


@frappe.whitelist()
def credits_balance():
    """Current credit state for the calling user. Used by the UI to render the
    credit chip on the AI hub + the paywall sheets.
    """
    try:
        _require_auth()
    except frappe.AuthenticationError:
        return _error_response("UNAUTHORIZED", "Authentication required", 401)
    try:
        return _get_credit_state(frappe.session.user)
    except Exception:
        corr = _log_unhandled("v1.credits_balance")
        return _error_response("SERVER_ERROR", "Failed to load credits", 500, correlation_id=corr)


@frappe.whitelist(methods=["POST"])
def dev_grant_credits(**kwargs):
    """ADMIN-ONLY hook for manually seeding credits while we develop the IAP /
    RTDN integrations. Restricted to System Manager so it can't leak into
    production accidentally — the real grants come from `_grant_monthly_credits`
    on subscription renew and `_grant_topup_credits` on validated purchases.

    Body: {event_type, bucket, delta, reason?, idempotency_key?, target_user?}
    """
    try:
        _require_auth()
    except frappe.AuthenticationError:
        return _error_response("UNAUTHORIZED", "Authentication required", 401)
    if "System Manager" not in frappe.get_roles(frappe.session.user):
        return _error_response("FORBIDDEN", "System Manager role required", 403)

    data = _read_json_body() if not kwargs else kwargs
    if not isinstance(data, dict):
        return _error_response("INVALID_BODY", "Expected JSON object", 400)

    target = data.get("target_user") or frappe.session.user
    event_type = data.get("event_type") or "adjustment"
    bucket = data.get("bucket") or "topup"
    try:
        delta = int(data.get("delta") or 0)
    except (TypeError, ValueError):
        return _error_response("INVALID_PARAMS", "'delta' must be an integer", 400)
    if delta == 0:
        return _error_response("INVALID_PARAMS", "'delta' must be non-zero", 400)

    try:
        balance_after = _insert_ledger_event(
            target,
            event_type=event_type,
            bucket=bucket,
            delta=delta,
            reason=data.get("reason") or "Dev grant",
            idempotency_key=data.get("idempotency_key"),
            period_month=data.get("period_month")
                or (_current_period_month() if bucket == "monthly" else None),
        )
        frappe.db.commit()
        return {"ok": True, "balance_after": balance_after}
    except Exception:
        corr = _log_unhandled("v1.dev_grant_credits")
        return _error_response("SERVER_ERROR", "Failed to grant credits", 500, correlation_id=corr)

# ===========================================================================
# CREDIT GATING (PREM-4)
# ===========================================================================
# Wires the credit ledger from PREM-1 into the AI submit pipeline. Behaviour
# is gated by the `credit_gating` feature flag so the existing free-daily
# quota stays in force until we flip the switch. Once on, a submit costs:
#
#   cost = variations * (hd ? 2 : 1)
#
# Floor-plan analysis stays free (or limited by the existing daily quota) on
# both sides — it costs near-zero on the vendor side and is the hook product.
# ===========================================================================

_CREDIT_HD_MULTIPLIER = 2


def _credit_gating_enabled():
    """Cheap wrapper around the feature flag. Anything other than an enabled
    `credit_gating` flag returns False so legacy behaviour holds.
    """
    try:
        return _flag_enabled("credit_gating")
    except Exception:
        return False


def _credit_cost_for(tool_id, quality, variations):
    """Per-submit credit cost. Floor-plan analysis is free; everything else
    scales by variations and doubles for HD output.
    """
    if tool_id == "floor_plan":
        return 0
    try:
        v = max(1, min(4, int(variations or 1)))
    except (TypeError, ValueError):
        v = 1
    hd = (quality or "std").lower() == "hd"
    return v * (_CREDIT_HD_MULTIPLIER if hd else 1)

# ===========================================================================
# AUTH-1 (anon bootstrap)
# ===========================================================================
# First-launch handshake — no email / password / OAuth. Client posts its
# device UUID, server returns a Frappe API key+secret bound to a disabled-
# login Website User row (so password auth can't hit it). Subsequent calls
# use the returned token in the standard Authorization header.
#
# Idempotent: re-calling with the same `device_id` returns the same row's
# keys. The row's email is namespaced (`anon-<device_id>@buildcost.anon`) so
# it can never collide with a real user.
# ===========================================================================

_ANON_EMAIL_DOMAIN = "buildcost.anon"


@frappe.whitelist(allow_guest=True, methods=["POST"])
def anon_bootstrap(**kwargs):
    """Create / return the anonymous account keyed to this device.

    Body: {"device_id": "<uuid>"}
    Returns: {"anon_user_id", "api_key", "api_secret", "created"}
    """
    data = _read_json_body() if not kwargs else kwargs
    if not isinstance(data, dict):
        return _error_response("INVALID_BODY", "Expected JSON object", 400)

    device_id = (data.get("device_id") or "").strip()
    # Cheap sanity check — refuse obvious junk so we don't spawn anon rows
    # on every malformed call.
    if len(device_id) < 8 or len(device_id) > 128:
        return _error_response(
            "INVALID_PARAMS",
            "device_id must be a 8-128 char UUID-ish string",
            400,
        )
    if not all(c.isalnum() or c in "-_" for c in device_id):
        return _error_response(
            "INVALID_PARAMS",
            "device_id contains unsupported characters",
            400,
        )

    email = f"anon-{device_id}@{_ANON_EMAIL_DOMAIN}"
    try:
        user_name = frappe.db.get_value("User", {"email": email}, "name")
        created = False

        if not user_name:
            u = frappe.new_doc("User")
            u.email = email
            u.first_name = "Guest"
            u.user_type = "Website User"
            u.enabled = 1
            # No welcome mail to a fake address; no password sign-in.
            u.send_welcome_email = 0
            u.flags.no_welcome_mail = True
            u.insert(ignore_permissions=True)
            # Random unguessable password so the row exists but password
            # login is effectively unreachable. The API key is the auth path.
            u.new_password = frappe.generate_hash(length=48)
            u.save(ignore_permissions=True)
            user_name = u.name
            created = True

        # _user_keys returns a (api_key, api_secret) tuple.
        api_key, api_secret = _user_keys(user_name)

        # Grant the one-time welcome design credits, anchored on the device so a
        # given device can only ever claim them once (idempotent). Only on first
        # creation of this device row, and only within the per-IP daily cap, to
        # slow credit farming via rotating device_ids. A failed/denied grant
        # never blocks the auth handshake.
        if created and _welcome_ip_allowed():
            try:
                _grant_welcome_credits(
                    user_name,
                    idempotency_key=f"welcome:{device_id}",
                )
            except Exception:
                frappe.log_error(frappe.get_traceback(), "anon_bootstrap.welcome_grant")

        frappe.db.commit()
        return {
            "anon_user_id": user_name,
            "api_key": api_key,
            "api_secret": api_secret,
            "created": created,
        }
    except Exception:
        corr = _log_unhandled("v1.anon_bootstrap")
        return _error_response(
            "SERVER_ERROR",
            "Failed to bootstrap anon account",
            500,
            correlation_id=corr,
        )


# ===========================================================================
# PREM-2 (top-up grant)
# ===========================================================================
# Client calls this after the Play store says a consumable was bought. The
# server records the grant in the credit ledger keyed on a hash of the
# receipt token so a retry can't double-credit.
#
# **TODO (AUTH-2 server tail):** actually validate `receipt_token` against
# the Play Developer API before granting. Today the endpoint trusts the
# client, which is fine for QA / dogfood with `iap_enabled` off in
# production — DO NOT ship this without the validation in place.
# ===========================================================================

# Authoritative price list — what each Play Console product is worth in
# credits. Mirror this when you flip `iap_enabled` on in production.
_TOPUP_GRANT_TABLE = {
    "credits_pack_starter_v1": 25,
    "credits_pack_plus_v1": 80,
    "credits_pack_pro_v1": 250,
}


def _topup_dedup_key(product_id, receipt_token, purchase_id):
    """Stable per-purchase key so re-submits don't double-grant."""
    src = f"{product_id}|{purchase_id or ''}|{receipt_token or ''}"
    return "topup:" + hashlib.sha256(src.encode("utf-8")).hexdigest()[:48]


@frappe.whitelist(methods=["POST"])
def grant_topup_credits(**kwargs):
    """Apply a topup grant for the calling user.

    Body: {"product_id", "receipt_token", "purchase_id"?, "platform"?}
    Returns: {"ok", "granted", "balance_after", "balance"}

    Idempotent on the (product_id + purchase_id + receipt_token) tuple.
    """
    try:
        _require_auth()
    except frappe.AuthenticationError:
        return _error_response("UNAUTHORIZED", "Authentication required", 401)

    data = _read_json_body() if not kwargs else kwargs
    if not isinstance(data, dict):
        return _error_response("INVALID_BODY", "Expected JSON object", 400)

    product_id = (data.get("product_id") or "").strip()
    if product_id not in _TOPUP_GRANT_TABLE:
        return _error_response(
            "UNKNOWN_PRODUCT",
            f"Top-up product {product_id!r} is not recognised",
            400,
        )

    receipt_token = (data.get("receipt_token") or "").strip()
    purchase_id = (data.get("purchase_id") or "").strip()
    if not receipt_token:
        return _error_response(
            "INVALID_PARAMS",
            "receipt_token is required",
            400,
        )

    # TODO(AUTH-2): replace this stub with a real Play Developer API call
    # (`purchases.products.get`) that verifies the receipt is genuine and
    # belongs to this device's obfuscatedAccountId before granting.

    grant_amount = _TOPUP_GRANT_TABLE[product_id]
    dedup_key = _topup_dedup_key(product_id, receipt_token, purchase_id)

    try:
        balance_after = _grant_topup_credits(
            frappe.session.user,
            amount=grant_amount,
            idempotency_key=dedup_key,
            reason=f"Top-up: {product_id}",
            ref_name=purchase_id or product_id,
        )
        frappe.db.commit()
        state = _get_credit_state(frappe.session.user)
        return {
            "ok": True,
            "granted": grant_amount,
            "balance_after": balance_after,
            "balance": state,
        }
    except Exception:
        corr = _log_unhandled("v1.grant_topup_credits")
        return _error_response(
            "SERVER_ERROR",
            "Failed to grant top-up credits",
            500,
            correlation_id=corr,
        )

