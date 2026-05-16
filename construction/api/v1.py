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
import traceback

import frappe

from construction.estimate_engine.engine import resolve_estimate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _error_response(code, message, http_status=400, details=None):
    """Return a structured error dict and set the HTTP status code."""
    frappe.local.response.http_status_code = http_status
    resp = {
        "ok": False,
        "error": {
            "code": code,
            "message": message,
        },
    }
    if details:
        resp["error"]["details"] = details
    return resp


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
        frappe.log_error(traceback.format_exc(), "v1.countries")
        return _error_response("SERVER_ERROR", "Failed to load countries", 500)


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
        frappe.log_error(traceback.format_exc(), "v1.cities")
        return _error_response("SERVER_ERROR", "Failed to load cities", 500)


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
        frappe.log_error(traceback.format_exc(), "v1.questionnaire")
        return _error_response("SERVER_ERROR", "Failed to load questionnaire", 500)


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
        frappe.log_error(traceback.format_exc(), "v1.estimate")
        return _error_response(
            "SERVER_ERROR",
            "An unexpected error occurred while computing the estimate",
            500,
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
        frappe.log_error(traceback.format_exc(), "v1.config")
        return _error_response("SERVER_ERROR", "Failed to load config", 500)


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
        frappe.log_error(traceback.format_exc(), "v1.login")
        return _error_response("SERVER_ERROR", "Login failed", 500)

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
        frappe.log_error(traceback.format_exc(), "v1.register")
        return _error_response("SERVER_ERROR", "Failed to create account", 500)

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
        frappe.log_error(traceback.format_exc(), "v1.save_estimate")
        return _error_response("SERVER_ERROR", "Failed to save estimate", 500)


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
        frappe.log_error(traceback.format_exc(), "v1.list_estimates")
        return _error_response("SERVER_ERROR", "Failed to list estimates", 500)


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
        frappe.log_error(traceback.format_exc(), "v1.delete_estimate")
        return _error_response("SERVER_ERROR", "Failed to delete estimate", 500)


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
            frappe.log_error(traceback.format_exc(), "v1.sync_estimates.item")
            failed.append({"client_id": est.get("client_id"), "reason": "server_error"})

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
# PHASE 2 - AI (floor plan + interior design)
# ============================================================================
#
# Architecture:
#   client -> POST analyze/generate (multipart for files, JSON for prompts)
#          -> AI Job doc created (status=queued) -> RQ worker
#          -> client polls /job_status until done|failed
#
# Kill switches: Feature Flag flag_key {ai_floor_plan, ai_interior_design}
#                + AI Settings.enabled + AI Settings.kill_switch_until
# Rate limit:    per-user per-minute, configured in AI Settings
# Budgets:       per-job-type and global daily/monthly caps in AI Settings.
#                Auto-trips kill switch when hit.
# Idempotency:   Idempotency-Key header (or body fallback). (user, key) is
#                unique on AI Job; duplicate submits return the original job.
# ============================================================================

from construction.construction.doctype.ai_settings import ai_settings as ai_cfg


_AI_FLAGS = {
    "floor_plan": "ai_floor_plan",
    "interior_design": "ai_interior_design",
}


def _flag_enabled(key):
    enabled = frappe.db.get_value("Feature Flag", key, "enabled")
    return bool(enabled)


def _rate_limit_check(user, job_type, limit_per_min):
    if not limit_per_min or limit_per_min <= 0:
        return True
    key = f"ai_rl:{job_type}:{user}"
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


def _canonical_request_hash(payload):
    canonical = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _spend_in_window(job_type, since):
    """Sum cost_cents across AI Jobs of [job_type] (or all when None)
    completed since [since]."""
    filters = {"completed_at": [">=", since]}
    if job_type:
        filters["job_type"] = job_type
    rows = frappe.get_all(
        "AI Job", filters=filters, fields=["cost_cents"], limit_page_length=0
    )
    return sum((r.get("cost_cents") or 0) for r in rows)


def _check_budgets(job_type, cfg):
    """Enforce per-job-type + global daily/monthly caps. Returns an error
    dict on breach (and trips the kill switch), or None on clear path."""
    from frappe.utils import add_to_date, now_datetime

    now = now_datetime()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = day_start.replace(day=1)

    # Per-job-type caps
    job_daily = cfg.get("daily_budget_cents") or 0
    job_monthly = cfg.get("monthly_budget_cents") or 0

    if job_daily:
        spent = _spend_in_window(job_type, day_start)
        if spent >= job_daily:
            ai_cfg.trip_kill_switch(
                hours=24,
                reason=f"{job_type} daily budget exceeded ({spent}/{job_daily}¢)",
            )
            return _error_response(
                "BUDGET_EXCEEDED",
                "Daily AI budget reached. Try again tomorrow.",
                429,
            )
    if job_monthly:
        spent = _spend_in_window(job_type, month_start)
        if spent >= job_monthly:
            # Trip until next month start.
            hours = max(1, int((add_to_date(month_start, months=1) - now).total_seconds() // 3600))
            ai_cfg.trip_kill_switch(
                hours=hours,
                reason=f"{job_type} monthly budget exceeded ({spent}/{job_monthly}¢)",
            )
            return _error_response(
                "BUDGET_EXCEEDED",
                "Monthly AI budget reached. Try again next month.",
                429,
            )

    # Global caps
    settings = ai_cfg.get_settings()
    g_daily = settings.global_daily_budget_cents or 0
    g_monthly = settings.global_monthly_budget_cents or 0

    if g_daily:
        spent = _spend_in_window(None, day_start)
        if spent >= g_daily:
            ai_cfg.trip_kill_switch(
                hours=24,
                reason=f"Global daily budget exceeded ({spent}/{g_daily}¢)",
            )
            return _error_response(
                "BUDGET_EXCEEDED", "Daily AI budget reached.", 429
            )
    if g_monthly:
        spent = _spend_in_window(None, month_start)
        if spent >= g_monthly:
            hours = max(1, int((add_to_date(month_start, months=1) - now).total_seconds() // 3600))
            ai_cfg.trip_kill_switch(
                hours=hours,
                reason=f"Global monthly budget exceeded ({spent}/{g_monthly}¢)",
            )
            return _error_response(
                "BUDGET_EXCEEDED", "Monthly AI budget reached.", 429
            )

    return None


def _idempotency_key_from_request(data):
    """Prefer the `Idempotency-Key` HTTP header (RFC draft), fall back to a
    body field for legacy clients."""
    if frappe.request and frappe.request.headers:
        hdr = frappe.request.headers.get("Idempotency-Key") or frappe.request.headers.get("X-Idempotency-Key")
        if hdr:
            return hdr.strip()
    return data.get("idempotency_key") if isinstance(data, dict) else None


def _save_uploaded_image():
    """If the request contains a multipart file under 'image' or 'file',
    persist it as a private File and return its file_url. Returns None
    when no file is attached."""
    if not (frappe.request and frappe.request.files):
        return None
    upload = frappe.request.files.get("image") or frappe.request.files.get("file")
    if not upload:
        return None

    content = upload.read()
    if len(content) > 10 * 1024 * 1024:
        return _error_response("FILE_TOO_LARGE", "Image must be ≤ 10 MB", 413)

    from frappe.utils.file_manager import save_file
    file_doc = save_file(
        fname=upload.filename or "ai_upload.bin",
        content=content,
        dt=None,
        dn=None,
        is_private=1,
    )
    return file_doc.file_url


def _normalize_status(s):
    """Translate server-side 'succeeded' to the client-facing 'done' the
    Flutter app expects."""
    return "done" if s == "succeeded" else s


def _ai_submit(job_type, extra_payload):
    """Shared submit pipeline for both AI flows."""
    try:
        _require_auth()
    except frappe.AuthenticationError:
        return _error_response("UNAUTHORIZED", "Authentication required", 401)

    if not ai_cfg.is_master_enabled():
        return _error_response("AI_DISABLED", "AI is currently disabled", 503)

    flag = _AI_FLAGS.get(job_type)
    if not flag or not _flag_enabled(flag):
        return _error_response(
            "AI_DISABLED",
            f"AI feature '{job_type}' is currently unavailable",
            503,
        )

    cfg = ai_cfg.get_job_config(job_type)

    if not _rate_limit_check(
        frappe.session.user, job_type, cfg.get("rate_limit_per_min")
    ):
        return _error_response(
            "RATE_LIMITED",
            "You're sending requests too fast. Please slow down.",
            429,
        )

    budget_err = _check_budgets(job_type, cfg)
    if budget_err is not None:
        return budget_err

    body = _read_json_body() or {}
    idempotency_key = _idempotency_key_from_request(body)
    if not idempotency_key:
        return _error_response(
            "MISSING_PARAMS",
            "Idempotency-Key header is required (client-generated UUID).",
        )

    existing = frappe.db.get_value(
        "AI Job",
        {"user": frappe.session.user, "idempotency_key": idempotency_key},
        "name",
    )
    if existing:
        return _ai_status_payload(existing)

    payload = {k: v for k, v in body.items() if k != "idempotency_key"}
    payload.update(extra_payload or {})

    try:
        doc = frappe.new_doc("AI Job")
        doc.job_type = job_type
        doc.status = "queued"
        doc.user = frappe.session.user
        doc.idempotency_key = idempotency_key
        doc.request_hash = _canonical_request_hash(payload)
        doc.request_payload_json = json.dumps(payload)
        doc.insert(ignore_permissions=True)
        frappe.db.commit()

        frappe.enqueue(
            "construction.api.ai_worker.run_job",
            queue="long",
            timeout=600,
            name=doc.name,
        )

        return _ai_status_payload(doc.name)
    except Exception:
        frappe.log_error(traceback.format_exc(), f"v1.ai_submit:{job_type}")
        return _error_response("SERVER_ERROR", "Failed to enqueue AI job", 500)


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
        "started_at": str(doc.started_at) if doc.started_at else None,
        "completed_at": str(doc.completed_at) if doc.completed_at else None,
    }


@frappe.whitelist(methods=["POST"])
def ai_floor_plan_analyze(**kwargs):
    """Submit a floor-plan image for AI analysis.

    Multipart fields: image=<file>, city=<str>, quality_level=<str>
    Header: Idempotency-Key: <uuid>
    """
    image_url = _save_uploaded_image()
    if isinstance(image_url, dict) and image_url.get("ok") is False:
        return image_url  # forwarded error response

    extras = {}
    if image_url:
        extras["image_url"] = image_url
    if frappe.request and frappe.request.form:
        for key in ("city", "country", "quality_level"):
            val = frappe.request.form.get(key)
            if val:
                extras[key] = val
    return _ai_submit("floor_plan", extras)


@frappe.whitelist(methods=["POST"])
def ai_interior_generate(**kwargs):
    """Submit an interior-design generation request.

    Body (JSON): {room_type, style, color_palette?, notes?}
    Header: Idempotency-Key: <uuid>
    """
    return _ai_submit("interior_design", None)


@frappe.whitelist()
def job_status(id=None, job_id=None):
    """Unified poll endpoint. Accepts either ?id=<> or ?job_id=<>."""
    try:
        _require_auth()
    except frappe.AuthenticationError:
        return _error_response("UNAUTHORIZED", "Authentication required", 401)
    name = id or job_id
    if not name:
        return _error_response("MISSING_PARAMS", "'id' is required")
    if not frappe.db.exists("AI Job", name):
        return _error_response("NOT_FOUND", "Job not found", 404)
    return _ai_status_payload(name)


# ── Backwards-compat aliases (old method names from Phase-2 prototype) ──
ai_floor_plan_submit = ai_floor_plan_analyze
ai_interior_submit = ai_interior_generate


@frappe.whitelist()
def ai_floor_plan_status(job_id=None):
    return job_status(job_id=job_id)


@frappe.whitelist()
def ai_interior_status(job_id=None):
    return job_status(job_id=job_id)
