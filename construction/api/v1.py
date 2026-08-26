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
import re
import sys
import traceback
import uuid
from contextlib import contextmanager

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

# Requests per rolling minute allowed per caller on /v1/estimate. The client
# LIVE bar debounces at 450 ms, so a very active user peaks well under this;
# the cap exists to stop a guest scripting the open endpoint.
ESTIMATE_RATE_LIMIT_PER_MIN = 60


def _estimate_rate_limited():
    # True when the caller exceeded ESTIMATE_RATE_LIMIT_PER_MIN. Keyed by
    # session user, falling back to the client IP for guests. Fails OPEN: a
    # cache outage must never take the estimate endpoint down with it, and
    # this endpoint spends no vendor money.
    #
    # `_client_ip()` rather than `frappe.local.request_ip`: the latter is taken
    # from a caller-supplied `X-Forwarded-For` with no trusted-proxy list, so
    # every guest could pick their own bucket and the limit bounded nothing.
    try:
        user = getattr(getattr(frappe, "session", None), "user", None)
        if user and user != "Guest":
            ident = user
        else:
            ident = _client_ip() or "unknown"
        current = _counter_incr(f"est_rl2:{ident}", 60)
        if current is None:
            return False
        return current > ESTIMATE_RATE_LIMIT_PER_MIN
    except Exception:
        return False


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

    import math
    if not (math.isfinite(plot_size) and math.isfinite(covered_area)):
        return _error_response(
            "INVALID_PARAMS",
            "'plot_size_sqft' and 'covered_area_sqft' must be finite numbers",
        )

    if plot_size <= 0 or covered_area <= 0:
        return _error_response(
            "INVALID_PARAMS",
            "'plot_size_sqft' and 'covered_area_sqft' must be positive",
        )

    if not isinstance(answers, dict):
        return _error_response("INVALID_PARAMS", "'answers' must be a JSON object")

    if _estimate_rate_limited():
        return _error_response(
            "RATE_LIMITED",
            "Too many estimate requests. Please slow down.",
            429,
        )

    # ── Premium gate (defense-in-depth) ──
    # Custom material rate overrides are a Pro feature. The client hides the UI
    # for non-premium users and re-gates before sending, but a tampered client
    # could still inject `rate_overrides` into `answers`. Strip them unless the
    # authenticated caller is premium. Anonymous/guest callers are never premium
    # (is_premium returns False for "Guest"), so free estimates ignore overrides.
    # Whatever happens is echoed back as `overrides_applied` so the client can
    # tell the user instead of silently pricing with defaults.
    overrides_requested = bool(
        isinstance(answers.get("rate_overrides"), dict) and answers["rate_overrides"]
    )
    overrides_applied = overrides_requested
    if overrides_requested:
        from construction.construction.doctype.ai_settings import ai_settings as ai_cfg
        if not ai_cfg.is_premium(frappe.session.user):
            answers = {k: v for k, v in answers.items() if k != "rate_overrides"}
            overrides_applied = False

    # ── Compute ──
    try:
        result = resolve_estimate(country, city, plot_size, covered_area, answers)
        if overrides_requested:
            result["overrides_applied"] = overrides_applied
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

        # `premium_active` is PER-USER server truth, not a rollout flag: the
        # client keys every Pro gate (HD, variations, custom rates, PDF quota,
        # ad removal) off this. The endpoint is allow_guest, but the app sends
        # its anon/session token, so frappe resolves the real caller here.
        #
        # A `premium_active` Feature Flag ROW is ignored here on purpose. It
        # used to OR into this value as a "global QA override", which meant one
        # enabled row handed client-side Pro to every user of the app — a
        # single mis-click in the desk UI giving away the product. Entitlement
        # comes from the premium table and nowhere else.
        flags.pop("premium_active", None)
        try:
            user = frappe.session.user
            if user and user != "Guest":
                from construction.construction.doctype.ai_settings import (
                    ai_settings as _ai_cfg,
                )
                flags["premium_active"] = bool(_ai_cfg.is_premium(user))
            else:
                flags["premium_active"] = False
        except Exception:
            # Entitlement lookup must never break config delivery; the client
            # falls back to its cached/default (non-premium) value.
            flags["premium_active"] = False

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


def _user_keys(user, rotate=False):
    """Return (api_key, api_secret) for a User, generating them on demand.

    `rotate` defaults to False. It used to be unconditional, which turned any
    successful re-issue into a permanent, irreversible lockout of whoever held
    the previous secret — so a stolen re-issue didn't just clone an account,
    it took it away from its owner with no path back. Existing secrets are now
    read back out of the password store and returned unchanged; a new one is
    minted only when the account has none, or when a caller explicitly asks
    for a rotation.
    """
    from frappe.utils.password import get_decrypted_password

    user_doc = frappe.get_doc("User", user)
    dirty = False
    if not user_doc.api_key:
        user_doc.api_key = frappe.generate_hash(length=15)
        dirty = True

    api_secret = None
    if not rotate:
        try:
            api_secret = get_decrypted_password(
                "User", user, fieldname="api_secret", raise_exception=False
            )
        except Exception:
            api_secret = None

    if not api_secret:
        api_secret = frappe.generate_hash(length=15)
        user_doc.api_secret = api_secret
        dirty = True

    if dirty:
        user_doc.save(ignore_permissions=True)
    return user_doc.api_key, api_secret


# Email/password `login` and `register` used to sit here as guest-allowed
# endpoints. Nothing ever called them — the app is anonymous-only and has no
# sign-in screen — and `register` was an uncapped way to create `User` rows.
# The implementations are parked, undecorated and unreachable, in
# `construction/api/legacy_auth.py`; that file's docstring lists what has to
# be fixed before either goes back on the wire. Account creation now happens
# only through `anon_bootstrap` below.


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


def _require_sync_entitlement():
    """Cloud sync is sold as a Pro feature; enforce that here, not only in the
    client.

    The client gates on its own `premium_active` flag, and until F-51 was fixed
    the sync path never ran at all — so nothing had ever exercised the server
    side of the gate. Now that it does run, the client flag would be the only
    thing standing between a tampered build and free cloud storage.

    `server_save` mirrors the client's QA/dogfood override so the two agree.
    """
    if ai_cfg.is_premium(frappe.session.user):
        return None
    try:
        if _flag_enabled("server_save"):
            return None
    except Exception:
        pass
    return _error_response(
        "PREMIUM_REQUIRED",
        "Cloud sync is a Pro feature. Subscribe to back up across devices.",
        402,
    )


def _require_auth():
    if frappe.session.user == "Guest":
        frappe.throw("Authentication required", frappe.AuthenticationError)


def _client_ip():
    """Best-available client IP, trusting only as many proxy hops as the site
    actually has in front of it.

    Frappe fills `frappe.local.request_ip` from `X-Forwarded-For`
    unconditionally, with no trusted-proxy list — so any caller can set it to
    whatever they like, which silently nullifies every IP-keyed control built
    on top of it. We trust exactly `trusted_proxy_hops` entries from the RIGHT
    of the header (site_config; default 1, the usual single nginx) and fall
    back to the socket peer whenever the header can't account for them.

    Set `trusted_proxy_hops: 0` in site_config when the app is served directly
    with nothing in front of it.
    """
    req = getattr(frappe, "request", None)
    if req is None:
        return None
    peer = getattr(req, "remote_addr", None)
    try:
        hops = int(frappe.get_site_config().get("trusted_proxy_hops", 1))
    except (TypeError, ValueError):
        hops = 1
    if hops <= 0:
        return peer
    chain = [p.strip() for p in (req.headers.get("X-Forwarded-For") or "").split(",") if p.strip()]
    idx = len(chain) - hops
    if idx < 0:
        # The caller sent fewer hops than our own proxies are supposed to add,
        # so the header is entirely caller-supplied. Ignore it.
        return peer
    return chain[idx]


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
    """The request BODY — JSON when the client sent JSON, otherwise the posted
    form fields. Returns None when a JSON body was announced but is malformed.

    Deliberately does not fall back to `frappe.local.form_dict`: Frappe merges
    query-string arguments into that dict, so `?receipt=...` on a GET was
    indistinguishable from a field in a POST body. Anything that decides
    whether to move money has to know which of the two it is looking at.
    """
    req = getattr(frappe, "request", None)
    if req is None:
        return frappe.local.form_dict if frappe.local.form_dict else {}
    if req.is_json:
        try:
            raw = req.data
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            return frappe.parse_json(raw) or {}
        except Exception:
            return None
    try:
        if req.form:
            return dict(req.form)
    except Exception:
        pass
    return {}


@frappe.whitelist(methods=["POST"])
def save_estimate(**kwargs):
    """Persist a single estimate. Idempotent on client_id."""
    try:
        _require_auth()
    except frappe.AuthenticationError:
        return _error_response("UNAUTHORIZED", "Authentication required", 401)

    gate = _require_sync_entitlement()
    if gate is not None:
        return gate

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
def delete_estimate(name=None, client_id=None):
    try:
        _require_auth()
    except frappe.AuthenticationError:
        return _error_response("UNAUTHORIZED", "Authentication required", 401)

    data = _read_json_body() or {}
    name = name or data.get("name")
    client_id = client_id or data.get("client_id")
    # Rows created via sync/save are keyed by client_id on the device; let the
    # client delete by client_id when it never learned the server doc name.
    if not name and client_id:
        name = frappe.db.get_value(
            "Estimate History",
            {"client_id": client_id, "user": frappe.session.user},
            "name",
        )
        if not name:
            # Nothing on the server for this client_id - treat as success so
            # a local-only delete is idempotent.
            return {"ok": True, "not_found": True}
    if not name:
        return _error_response("MISSING_PARAMS", "'name' or 'client_id' is required")

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

    gate = _require_sync_entitlement()
    if gate is not None:
        return gate

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

# C2: tools that support mode=text (design from imagination, no photo) and
# mode=sketch (render a hand drawing). The editing tools need a real photo.
_TEXT_MODE_TOOLS = {"interior", "exterior", "garden"}


def _flag_enabled(key):
    enabled = frappe.db.get_value("Feature Flag", key, "enabled")
    return bool(enabled)


def _counter_incr(key, ttl_seconds):
    """Atomically bump a Redis counter and return its new value.

    The previous get-then-set form was a read-modify-write across a network
    round trip: N concurrent requests all read the same value and all wrote
    the same value + 1, so a limit of 5 admitted as many requests as the
    client could open at once. `INCR` is atomic server-side, so the Nth caller
    genuinely sees N. Raw redis commands need the key namespaced by hand —
    `set_value` does that for us, `incrby` does not — and the counter keys are
    deliberately distinct from any `set_value` key because those values are
    pickled and would blow up an INCR.

    Returns None when the cache is unreachable so callers can decide whether
    that means allow or deny; there is no safe universal default here.
    """
    try:
        cache = frappe.cache()
        rkey = cache.make_key(key)
        current = cache.incrby(rkey, 1)
        if int(current) == 1:
            cache.expire(rkey, int(ttl_seconds))
        return int(current)
    except Exception:
        return None


def _counter_decr(key):
    """Give back a slot taken by `_counter_incr` (compensating a rejected or
    failed request). Best-effort — a lost decrement only costs the user one
    slot until the window rolls."""
    try:
        cache = frappe.cache()
        cache.decrby(cache.make_key(key), 1)
    except Exception:
        pass


def _rate_limit_check(user, tool_id, limit_per_min):
    if not limit_per_min or limit_per_min <= 0:
        return True
    current = _counter_incr(f"ai_rl2:{tool_id}:{user}", 60)
    if current is None:
        # Cache down. This limiter exists to stop abusive bursts, not to gate
        # money (the credit ledger does that), so a Redis outage should not
        # take the product offline.
        return True
    return current <= limit_per_min


def _free_quota_key(user):
    from frappe.utils import nowdate
    return f"ai_free2:{user}:{nowdate()}"


def _free_quota_claim(user, tool_id, free_daily_limit):
    """Claim one slot of today's free quota for a non-premium user.

    Returns (ok, remaining_today). This *reserves* the slot up front rather
    than counting it after the job is enqueued: the old order (check, run,
    then increment) let concurrent submits all pass the check before any of
    them incremented, and a crash between the two lost the count entirely.
    Callers must call `_free_quota_release` if they end up not running the
    job, so a rejected submit doesn't burn the user's day.

    Premium bypasses the cap.
    """
    if not free_daily_limit or free_daily_limit <= 0:
        return True, -1
    if ai_cfg.is_premium(user):
        return True, -1
    current = _counter_incr(_free_quota_key(user), 60 * 60 * 36)
    if current is None:
        # Cache down: this counter is the only bound on free generations, and
        # it is spending real vendor money. Fail closed.
        return False, 0
    if current > free_daily_limit:
        _counter_decr(_free_quota_key(user))
        return False, 0
    return True, free_daily_limit - current


def _free_quota_release(user):
    _counter_decr(_free_quota_key(user))


def _canonical_request_hash(payload):
    canonical = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _worst_case_cost_cents(tool_id):
    """The tool's own per-job ceiling, for pricing work that hasn't billed yet."""
    try:
        return int(ai_cfg.get_job_config(tool_id).get("max_cost_cents") or 0) or 25
    except Exception:
        return 25


def _spend_in_window(tool_id, since):
    """Vendor spend attributable to this window, INCLUDING work in flight.

    Counting only `completed_at` made the cap blind to precisely the spend it
    exists to bound: a burst of submits all pass the check because none of them
    has finished yet, all of them run, and the bill lands afterwards. In-flight
    jobs are priced at the tool's `max_cost_cents` ceiling — the honest worst
    case, and the number a cap should be reasoning about.
    """
    filters = {"completed_at": [">=", since]}
    if tool_id:
        filters["job_type"] = tool_id
    rows = frappe.get_all(
        "AI Job", filters=filters, fields=["cost_cents"], limit_page_length=0
    )
    spent = sum((r.get("cost_cents") or 0) for r in rows)

    # Only work the watchdog would still consider alive. An unbounded
    # `queued`/`running` filter also counted jobs that had been stuck for
    # weeks, so a single wedged row inflated the cap permanently and paused
    # the free tier for everyone with no live spend behind it. Past
    # `_STALE_QUEUED_HOURS` a job is the watchdog's problem, not the budget's.
    from frappe.utils import add_to_date, now_datetime

    inflight_floor = max(since, add_to_date(now_datetime(), hours=-_STALE_QUEUED_HOURS))
    inflight_filters = {
        "status": ["in", ["queued", "running"]],
        "creation": [">=", inflight_floor],
    }
    if tool_id:
        inflight_filters["job_type"] = tool_id
    inflight = frappe.get_all(
        "AI Job", filters=inflight_filters, fields=["job_type"], limit_page_length=0
    )
    ceilings = {}
    for r in inflight:
        jt = r.get("job_type")
        if jt not in ceilings:
            ceilings[jt] = _worst_case_cost_cents(jt)
        spent += ceilings[jt]
    return spent


def _budget_stop(scope, hours, reason, is_premium, message):
    """Apply a budget breach and decide whether THIS caller is stopped by it.

    Free work pauses; a subscriber or a credit-holder keeps going, because
    they have already paid for the generation they are asking for and the
    breach is the operator's problem to price, not theirs to absorb.
    """
    ai_cfg.pause_free_tier(scope, hours, reason)
    if is_premium:
        return None
    return _error_response("BUDGET_EXCEEDED", message, 429)


def _check_budgets(tool_id, cfg, is_premium=False):
    from frappe.utils import add_to_date, now_datetime

    now = now_datetime()

    # Already paused from an earlier breach in this window — no need to
    # re-scan the job table to find out.
    for scope in (tool_id, "global"):
        if ai_cfg.free_tier_paused(scope) and not is_premium:
            return _error_response(
                "BUDGET_EXCEEDED",
                "Free AI generations have paused for now. "
                "Subscribe or top up to keep going.",
                429,
            )
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = day_start.replace(day=1)

    job_daily = cfg.get("daily_budget_cents") or 0
    job_monthly = cfg.get("monthly_budget_cents") or 0

    if job_daily:
        spent = _spend_in_window(tool_id, day_start)
        if spent >= job_daily:
            err = _budget_stop(
                tool_id, 24,
                f"{tool_id} daily budget exceeded ({spent}/{job_daily}c)",
                is_premium,
                "Free AI generations have paused for today. "
                "Subscribe or top up to keep going.",
            )
            if err is not None:
                return err
    if job_monthly:
        spent = _spend_in_window(tool_id, month_start)
        if spent >= job_monthly:
            hours = max(1, int((add_to_date(month_start, months=1) - now).total_seconds() // 3600))
            err = _budget_stop(
                tool_id, hours,
                f"{tool_id} monthly budget exceeded ({spent}/{job_monthly}c)",
                is_premium,
                "Free AI generations have paused for this month. "
                "Subscribe or top up to keep going.",
            )
            if err is not None:
                return err

    settings = ai_cfg.get_settings()
    g_daily = settings.global_daily_budget_cents or 0
    g_monthly = settings.global_monthly_budget_cents or 0

    if g_daily:
        spent = _spend_in_window(None, day_start)
        if spent >= g_daily:
            err = _budget_stop(
                "global", 24,
                f"Global daily budget exceeded ({spent}/{g_daily}c)",
                is_premium,
                "Free AI generations have paused for today. "
                "Subscribe or top up to keep going.",
            )
            if err is not None:
                return err
    if g_monthly:
        spent = _spend_in_window(None, month_start)
        if spent >= g_monthly:
            hours = max(1, int((add_to_date(month_start, months=1) - now).total_seconds() // 3600))
            err = _budget_stop(
                "global", hours,
                f"Global monthly budget exceeded ({spent}/{g_monthly}c)",
                is_premium,
                "Free AI generations have paused for this month. "
                "Subscribe or top up to keep going.",
            )
            if err is not None:
                return err

    return None


def _idempotency_key_from_request(data):
    if frappe.request and frappe.request.headers:
        hdr = frappe.request.headers.get("Idempotency-Key") or frappe.request.headers.get("X-Idempotency-Key")
        if hdr:
            return hdr.strip()
    return data.get("idempotency_key") if isinstance(data, dict) else None


def _strip_image_metadata(content):
    """Re-encode an uploaded image without EXIF/GPS metadata (A4).

    Uploaded home photos routinely carry the GPS coordinates of the user's
    home address in EXIF; that must never reach disk or a third-party vendor.
    Re-encoding through Pillow drops every metadata block. Orientation is
    baked into the pixels first so losing the Orientation tag can't rotate
    the image. Fails CLOSED: content we can't parse and re-encode (HEIC,
    decompression bombs, corrupt files) is rejected with an error-response
    dict instead of passed through with its metadata intact (L1)."""
    import io as _io
    from PIL import Image, ImageOps

    try:
        img = Image.open(_io.BytesIO(content))
        fmt = (img.format or "").upper()
        if fmt not in ("JPEG", "PNG", "WEBP"):
            return _error_response(
                "UNSUPPORTED_IMAGE",
                "Unsupported image format — please upload a JPEG, PNG, or WebP photo.",
                400,
            )
        # Explicit pixel cap below PIL's ~179MP bomb threshold: a 10 MB PNG
        # can decompress to hundreds of MB of RAM. 50MP clears every phone
        # camera export while bounding the re-encode cost.
        if img.width * img.height > 50_000_000:
            return _error_response(
                "IMAGE_TOO_LARGE",
                "That image has too many pixels. Please upload a smaller photo.",
                413,
            )
        img = ImageOps.exif_transpose(img)
        out = _io.BytesIO()
        if fmt == "JPEG":
            img.convert("RGB").save(out, format="JPEG", quality=92)
        elif fmt == "PNG":
            img.save(out, format="PNG")
        else:
            img.save(out, format="WEBP", quality=92)
        return out.getvalue()
    except Image.DecompressionBombError:
        return _error_response(
            "IMAGE_TOO_LARGE",
            "That image has too many pixels. Please upload a smaller photo.",
            413,
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "v1.exif_strip_failed")
        return _error_response(
            "INVALID_IMAGE",
            "We couldn't read that image. Please upload a JPEG, PNG, or WebP photo.",
            400,
        )


def _submit_preflight(tool_id):
    """Cheap gate to run BEFORE any upload is persisted.

    Uploads were being written to disk — and left there — before authentication
    was even checked, so an unauthenticated caller could fill the disk 10 MB at
    a time and nothing ever swept the orphans. This answers the questions that
    don't need the request body: is the caller real, is the tool live, are they
    inside their rate limit. The authoritative checks still happen under the
    ledger lock in `_ai_submit`.

    Returns an error response, or None to proceed.
    """
    try:
        _require_auth()
    except frappe.AuthenticationError:
        return _error_response("UNAUTHORIZED", "Authentication required", 401)
    if not ai_cfg.is_master_enabled():
        return _error_response("AI_DISABLED", "AI is currently disabled", 503)
    flag = _AI_FLAGS.get(tool_id)
    if not flag or not _flag_enabled(flag):
        return _error_response(
            "AI_DISABLED", f"AI tool {tool_id} is currently unavailable", 503
        )
    if ai_cfg.is_tool_tripped(tool_id):
        return _error_response(
            "AI_DISABLED",
            "This tool is briefly unavailable while we recover. Please try again shortly.",
            503,
        )
    return None


def _discard_uploads(*urls):
    """Delete private uploads belonging to a submit that never became a job."""
    for url in urls:
        if url and isinstance(url, str):
            try:
                _delete_private_file(url)
            except Exception:
                frappe.log_error(frappe.get_traceback(), "v1.discard_uploads")


def _save_uploaded_file(field_name):
    """If the request has a multipart file under [field_name], persist it as a
    private File and return its file_url. Returns None when absent, or a
    forwarded error response dict when oversized. EXIF/GPS metadata is
    stripped before the bytes ever hit disk (A4)."""
    if not (frappe.request and frappe.request.files):
        return None
    upload = frappe.request.files.get(field_name)
    if not upload:
        return None
    content = upload.read()
    if len(content) > 10 * 1024 * 1024:
        return _error_response("FILE_TOO_LARGE", "Image must be 10 MB or less", 413)
    content = _strip_image_metadata(content)
    if isinstance(content, dict):
        return content
    from frappe.utils.file_manager import save_file
    file_doc = save_file(
        fname=upload.filename or f"ai_{field_name}.bin",
        content=content,
        dt=None,
        dn=None,
        is_private=1,
    )
    return file_doc.file_url


_NOTES_SUBMIT_MAX_LEN = 300


def _validate_notes(body):
    """Submit-side guard on the free-text `notes` field (A2).

    Over-long notes are truncated silently (friendlier than an error);
    blocklisted notes are rejected outright BEFORE any credit is debited.
    The worker re-sanitizes as defense-in-depth for legacy/replayed payloads."""
    notes = body.get("notes")
    if not notes:
        return None
    notes = str(notes)
    if len(notes) > _NOTES_SUBMIT_MAX_LEN:
        notes = notes[:_NOTES_SUBMIT_MAX_LEN]
        body["notes"] = notes
    from construction.api.ai_worker import notes_blocked_term
    if notes_blocked_term(notes):
        return _error_response(
            "NOTES_REJECTED",
            "Your notes contain content we can't process. "
            "Please describe the design change you want and try again.",
            400,
        )
    return None


def _resolve_refine_source(body):
    """C1 (multi-turn refine): resolve source_job_id/source_variation into the
    prior result's private file URL, so the new run edits the AI's own output
    instead of restarting from the original photo.

    Premium-only: free-tier results carry a burned-in watermark, so refining
    them would compound watermarks into the new image.

    Returns a file_url string, None when no source_job_id given, or an error
    response dict."""
    job_id = body.get("source_job_id")
    if not job_id:
        return None
    if not frappe.db.exists("AI Job", job_id):
        return _error_response("NOT_FOUND", "Source job not found", 404)
    job = frappe.get_doc("AI Job", job_id)
    if job.user != frappe.session.user:
        return _error_response("FORBIDDEN", "You do not own this job", 403)
    if job.status != "succeeded":
        return _error_response("NOT_READY", "Source job has not finished", 409)
    if not ai_cfg.is_premium(frappe.session.user):
        return _error_response(
            "PREMIUM_REQUIRED", "Refining a result is a premium feature.", 402
        )
    result = json.loads(job.result_json or "null") or {}
    images = result.get("images") or []
    try:
        idx = int(body.get("source_variation") or 0)
    except (TypeError, ValueError):
        idx = 0
    if idx < 0 or idx >= len(images):
        return _error_response("BAD_VARIATION", "source_variation out of range", 400)
    url = (images[idx] or {}).get("url")
    if not url:
        return _error_response("NO_RESULT", "Source variation has no image", 422)
    return url


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

    if ai_cfg.is_tool_tripped(tool_id):
        return _error_response(
            "AI_DISABLED",
            "This tool is briefly unavailable while we recover. Please try again shortly.",
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

    # A2: cap/validate free-text notes before any debit or vendor spend.
    notes_err = _validate_notes(body)
    if notes_err is not None:
        return notes_err

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

    # Everything from here to the job insert is one critical section per user.
    # Without it the retry short-circuit races the insert (two jobs, one
    # charge) and the debit races itself (two jobs, one credit). The lock is
    # per-user, so it costs nothing in the common case.
    try:
        with _user_ledger_lock(user):
            return _ai_submit_locked(
                tool_id, body, extra_payload, user, cfg, is_premium,
                quality, variations, idempotency_key,
            )
    except LedgerLockError:
        return _error_response(
            "BUSY",
            "Another request is still being processed. Please try again.",
            409,
        )


def _ai_submit_locked(
    tool_id, body, extra_payload, user, cfg, is_premium,
    quality, variations, idempotency_key,
):
    """The charge-and-enqueue half of `_ai_submit`, under the per-user ledger
    lock. Split out only so the lock's scope is unmistakable."""
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
    budget_err = _check_budgets(tool_id, cfg, is_premium=is_premium)
    if budget_err is not None:
        return budget_err

    # Every priced submit debits the credit ledger. There is no longer a flag
    # that can switch this off: `credit_gating` used to gate the whole block,
    # was never seeded, and a missing row read as "free" — so the metered
    # product was off by default anywhere nobody had created the row by hand.
    debit_key = None
    debit_result = None
    # Zero-cost tools (floor plan) can't be bounded by a debit of 0, so they
    # stay on the per-day free quota instead. That is not a fallback; it is
    # how a tool that costs nothing is kept finite.
    charge_free_quota = False
    cost = _credit_cost_for(tool_id, quality, variations)
    if cost > 0:
        debit_key = _idem("ai_submit", user, idempotency_key)
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
        charge_free_quota = True

    if charge_free_quota:
        # Reserve the slot now. Counting it after the enqueue let concurrent
        # submits all pass the check before any of them incremented; the
        # release below gives it back on every path that doesn't run a job.
        ok, remaining = _free_quota_claim(user, tool_id, cfg.get("free_daily_limit"))
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

    job_name = None
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
        # F-42: freeze the entitlement the job was PRICED with. The worker used
        # to re-derive premium minutes later, so a subscription lapsing while
        # the job sat in the queue burned a watermark into an image the user
        # had paid full Pro price for — irreversibly, with no refund path and
        # no record that it happened.
        doc.charged_as_premium = 1 if is_premium else 0
        doc.source_image_url = extra_payload.get("image_url") if extra_payload else None
        doc.mask_url = extra_payload.get("mask_url") if extra_payload else None
        doc.ref_image_url = extra_payload.get("ref_image_url") if extra_payload else None
        doc.insert(ignore_permissions=True)
        job_name = doc.name

        # PREM-4: now that the job has a name, point the credit-debit rows at
        # it so the worker can issue a refund-by-ref if the job fails. Scoped to
        # THIS submit's debit key so we never touch another job's ledger rows.
        if debit_key:
            try:
                m_key, t_key = _debit_sub_keys(debit_key)
                frappe.db.sql(
                    """update `tabAI Credit Ledger`
                       set ref_name = %(name)s
                       where user = %(user)s
                         and ref_doctype = 'AI Job'
                         and (ref_name is null or ref_name = '')
                         and idempotency_key in (%(m_key)s, %(t_key)s)""",
                    {
                        "name": doc.name,
                        "user": user,
                        "m_key": m_key,
                        "t_key": t_key,
                    },
                )
            except Exception:
                # Not fatal, but not harmless either: a refund that looks the
                # debit up by ref_name would find nothing. `_refund_job_credits`
                # recovers by recomputing the key, so log and carry on.
                frappe.log_error(
                    frappe.get_traceback(), "v1.ai_submit.ref_backfill_failed"
                )

        frappe.db.commit()

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
        #
        # When the job row exists (the common case — it is inserted and
        # committed before `enqueue` is even called) the compensation MUST go
        # through the same key namespace the worker and the watchdog use.
        # A separate reversal key deduplicated against neither, so an enqueue
        # failure refunded once here and again an hour later from the watchdog,
        # leaving the user a credit richer than before they submitted.
        if debit_result and debit_result.get("applied", 0) > 0:
            try:
                if job_name:
                    # Also mark it failed, so the watchdog isn't the thing that
                    # discovers an orphaned `queued` row an hour from now.
                    try:
                        frappe.db.set_value("AI Job", job_name, {
                            "status": "failed",
                            "error_code": "WORKER_ERROR",
                            "error_message": (
                                "We couldn't start this job. "
                                "Your credits have been refunded — please try again."
                            ),
                            "completed_at": frappe.utils.now_datetime(),
                        }, update_modified=False)
                    except Exception:
                        frappe.log_error(
                            frappe.get_traceback(), "v1.ai_submit.mark_failed"
                        )
                    _refund_job_credits(
                        user, job_name, idempotency_key,
                        reason="Submit failed before the job could be queued",
                    )
                else:
                    _reverse_debit(user, debit_key, debit_result)
                frappe.db.commit()
            except Exception:
                frappe.log_error(frappe.get_traceback(), "v1.ai_submit.reverse_failed")
        # Give the free-quota slot back too. Reversing only the credit debit
        # left free-tier users a day's quota short for a job that never ran.
        if charge_free_quota:
            _free_quota_release(user)
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
        image        <file>         required (except mode=text / refine)
        mode         photo | sketch | text   default photo (C2; text/sketch
                                    are interior/exterior/garden only)
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

    pre = _submit_preflight(tool_id)
    if pre is not None:
        return pre

    # C2: text (no source photo) and sketch (hand drawing → photoreal render)
    # modes, for the three "design a space" tools only — the editing tools
    # (mask/ref/paint/…) are meaningless without a real photo.
    mode = (body.get("mode") or "photo").strip().lower()
    if mode not in ("photo", "text", "sketch"):
        mode = "photo"
    if mode != "photo" and tool_id not in _TEXT_MODE_TOOLS:
        return _error_response(
            "MODE_UNSUPPORTED",
            "Starting from text or a sketch is only available for interior, "
            "exterior and garden designs.",
            400,
        )
    body["mode"] = mode

    image_url = None
    if mode != "text":
        image_url = _save_uploaded_file("image")
        if isinstance(image_url, dict) and image_url.get("ok") is False:
            return image_url
        if not image_url and mode == "photo":
            # C1: multi-turn refine — no fresh upload, edit a prior result
            # image. Photo mode only: a refine source is an AI render, and
            # feeding it through the sketch prompt ("this is a hand drawing")
            # would degrade the result.
            refined = _resolve_refine_source(body)
            if isinstance(refined, dict) and refined.get("ok") is False:
                return refined
            image_url = refined
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
        _discard_uploads(image_url, mask_url)
        return ref_url
    if ref_url:
        extras["ref_image_url"] = ref_url

    result = _ai_submit(tool_id, body, extras)
    # Nothing sweeps an upload whose submit was rejected — the retention task
    # only walks files reachable from an AI Job — so clean up here.
    if isinstance(result, dict) and result.get("ok") is False:
        _discard_uploads(image_url, mask_url, ref_url)
    return result


@frappe.whitelist(methods=["POST"])
def ai_floor_plan_analyze(**kwargs):
    """Submit a floor-plan image for AI analysis (vision JSON, not image edit).

    Multipart fields: image=<file>, city=<str>, quality_level=<str>
    Header: Idempotency-Key: <uuid>
    """
    pre = _submit_preflight("floor_plan")
    if pre is not None:
        return pre

    image_url = _save_uploaded_file("image")
    if isinstance(image_url, dict) and image_url.get("ok") is False:
        return image_url
    if not image_url:
        return _error_response("MISSING_IMAGE", "A floor plan image is required.", 400)

    extras = {"image_url": image_url}
    if frappe.request and frappe.request.form:
        for key in ("city", "country", "quality_level"):
            val = frappe.request.form.get(key)
            if val:
                extras[key] = val

    body = dict(frappe.request.form) if (frappe.request and frappe.request.form) else {}
    result = _ai_submit("floor_plan", body, extras)
    if isinstance(result, dict) and result.get("ok") is False:
        _discard_uploads(image_url)
    return result


@frappe.whitelist(methods=["POST"])
def ai_interior_generate(**kwargs):
    """Legacy interior endpoint - accepts JSON body without a source image.

    Kept for the legacy interior_design_page in the Flutter app. New clients
    should use /ai_generate with tool_id=interior and a source image.
    """
    body = _read_json_body() or {}
    return _ai_submit("interior", body, {})


@frappe.whitelist(methods=["GET"])
def job_status(id=None, job_id=None):
    """Unified poll endpoint. Accepts either ?id=<> or ?job_id=<>.

    A job the caller does not own is reported as NOT_FOUND, not FORBIDDEN.
    Splitting the two answers turns this endpoint into an existence oracle:
    with sequential job names (now `hash`, but the old rows remain) it let any
    account measure the platform's total generation volume, and the
    distinction buys the legitimate caller nothing.
    """
    try:
        _require_auth()
    except frappe.AuthenticationError:
        return _error_response("UNAUTHORIZED", "Authentication required", 401)
    name = id or job_id
    if not name:
        return _error_response("MISSING_PARAMS", "id is required")
    owner = frappe.db.get_value("AI Job", name, "user")
    if owner != frappe.session.user:
        return _error_response("NOT_FOUND", "Job not found", 404)
    return _ai_status_payload(name)


# Backwards-compat aliases
ai_floor_plan_submit = ai_floor_plan_analyze
ai_interior_submit = ai_interior_generate


# Frappe enforces `methods=[...]` in the dispatcher, not inside the function,
# so an alias that re-declares `@frappe.whitelist()` without the restriction
# routes around it entirely. These two must carry the same decorator as the
# endpoint they delegate to.
@frappe.whitelist(methods=["GET"])
def ai_floor_plan_status(job_id=None):
    return job_status(job_id=job_id)


@frappe.whitelist(methods=["GET"])
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

    try:
        idx = int(body.get("variation_index") or 0)
    except (TypeError, ValueError):
        idx = 0

    try:
        return _upsert_design_for_job(
            job,
            variation_index=idx,
            style=body.get("style"),
            room=body.get("room"),
            color=body.get("color"),
            notes=body.get("notes"),
        )
    except ValueError as e:
        if str(e) == "NO_RESULT":
            return _error_response(
                "NO_RESULT", "Selected variation has no image URL", 422
            )
        return _error_response("BAD_VARIATION", "variation_index out of range", 400)


def _design_payload(design):
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


def _upsert_design_for_job(job, variation_index=0, style=None, room=None,
                           color=None, notes=None):
    """Create the AI Design row for (job, variation) if it doesn't exist yet.

    Idempotent on (user, job_id, variation_index): the worker auto-saves
    variation 0 on every success (so a result reaches My Designs even when no
    client is watching — killed app, client poll timeout, backgrounded
    compare), and the client's explicit save lands on the same row instead of
    duplicating it.

    Raises ValueError("BAD_VARIATION"|"NO_RESULT") for unusable input;
    commits on insert.
    """
    result = json.loads(job.result_json or "null") or {}
    images = result.get("images") or []
    if variation_index < 0 or variation_index >= len(images):
        raise ValueError("BAD_VARIATION")
    sel = images[variation_index] if isinstance(images[variation_index], dict) else {}
    result_url = sel.get("url")
    if not result_url:
        raise ValueError("NO_RESULT")

    existing = frappe.db.get_value(
        "AI Design",
        {"user": job.user, "job_id": job.name, "variation_index": variation_index},
        "name",
    )
    if existing:
        return _design_payload(frappe.get_doc("AI Design", existing))

    req_payload = json.loads(job.request_payload_json or "{}")

    design = frappe.new_doc("AI Design")
    design.user = job.user
    design.tool_id = job.job_type
    design.job_id = job.name
    design.variation_index = variation_index
    design.style = style or req_payload.get("style")
    design.room = room or req_payload.get("room")
    design.color = color or req_payload.get("color")
    design.notes = notes or req_payload.get("notes")
    design.source_file = job.source_image_url
    design.result_file = result_url
    design.thumb_file = sel.get("thumb_url")
    design.params_json = json.dumps({**req_payload, "variation_index": variation_index})
    design.model = job.model
    design.seed = str(sel.get("seed")) if sel.get("seed") is not None else None
    design.insert(ignore_permissions=True)
    frappe.db.commit()
    return _design_payload(design)


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


@frappe.whitelist(methods=["POST"])
def ai_feedback(**kwargs):
    """Record a thumbs up/down on a generated result (C3).

    Body (JSON or form):
        job_id       required, must be owned by caller
        rating       "up" | "down"   required
        reason       optional short free text (capped at 300 chars)
        image_index  optional int 0..3 (which variation), default 0

    Upserts on (user, job_id, image_index) so tapping up then down flips the
    rating instead of double-counting.
    """
    try:
        _require_auth()
    except frappe.AuthenticationError:
        return _error_response("UNAUTHORIZED", "Authentication required", 401)

    body = _read_json_body() or {}
    if not body and frappe.request and frappe.request.form:
        body = dict(frappe.request.form)

    job_id = body.get("job_id")
    rating = (body.get("rating") or "").lower()
    if not job_id or rating not in ("up", "down"):
        return _error_response(
            "MISSING_PARAMS", "job_id and rating (up|down) are required", 400
        )

    if not frappe.db.exists("AI Job", job_id):
        return _error_response("NOT_FOUND", "Job not found", 404)
    job = frappe.get_doc("AI Job", job_id)
    if job.user != frappe.session.user:
        return _error_response("FORBIDDEN", "You do not own this job", 403)

    try:
        image_index = max(0, min(3, int(body.get("image_index") or 0)))
    except (TypeError, ValueError):
        image_index = 0
    reason = str(body.get("reason") or "").strip()[:300] or None

    req_payload = json.loads(job.request_payload_json or "{}")

    existing = frappe.db.get_value(
        "AI Feedback",
        {"user": frappe.session.user, "job_id": job_id, "image_index": image_index},
        "name",
    )
    if existing:
        fb = frappe.get_doc("AI Feedback", existing)
    else:
        fb = frappe.new_doc("AI Feedback")
        fb.user = frappe.session.user
        fb.job_id = job_id
        fb.image_index = image_index
    fb.tool_id = job.job_type
    fb.rating = rating
    fb.reason = reason
    fb.model = job.model
    fb.style = req_payload.get("style")
    fb.save(ignore_permissions=True)
    frappe.db.commit()
    return {"id": fb.name, "rating": fb.rating}


# ============================================================================
# Data governance (E1): retention + user-initiated deletion of source photos
# ============================================================================
#
# Source photos (users' actual homes: address hints, valuables, family
# members) are more sensitive than generated results. Results are kept
# indefinitely (V1 Q9); source uploads are deleted after AI_SOURCE_RETENTION_
# DAYS unless the user saved a design that still references them (the
# before/after slider needs the original), and can be purged on demand via
# delete_my_ai_photos.

AI_SOURCE_RETENTION_DAYS = 30


def _delete_private_file(file_url):
    """Delete every File row (and its disk content) matching file_url.
    Returns how many rows were deleted."""
    if not file_url:
        return 0
    deleted = 0
    for row in frappe.get_all("File", filters={"file_url": file_url}, pluck="name"):
        try:
            frappe.delete_doc("File", row, ignore_permissions=True, force=True)
            deleted += 1
        except Exception:
            frappe.log_error(frappe.get_traceback(), "v1.ai_photo_delete_failed")
    return deleted


def cleanup_ai_source_uploads():
    """Scheduled daily (hooks.py): enforce the source-photo retention window.

    Deletes source/mask/ref uploads on terminal AI Jobs older than
    AI_SOURCE_RETENTION_DAYS, except files still referenced by a saved
    AI Design. Clears the URL fields on the job so re-runs skip it. Bounded
    per run so a large backlog can't stall the scheduler."""
    from frappe.utils import add_days, now_datetime

    cutoff = add_days(now_datetime(), -AI_SOURCE_RETENTION_DAYS)
    kept = {
        r
        for r in frappe.get_all("AI Design", pluck="source_file", limit_page_length=0)
        if r
    }
    jobs = frappe.get_all(
        "AI Job",
        filters={
            "creation": ["<", cutoff],
            "status": ["in", ["succeeded", "failed"]],
        },
        or_filters=[
            ["source_image_url", "!=", ""],
            ["mask_url", "!=", ""],
            ["ref_image_url", "!=", ""],
        ],
        fields=["name", "source_image_url", "mask_url", "ref_image_url"],
        limit_page_length=500,
    )
    removed = 0
    for job in jobs:
        for field in ("source_image_url", "mask_url", "ref_image_url"):
            url = job.get(field)
            if url and url not in kept:
                removed += _delete_private_file(url)
            if url:
                frappe.db.set_value("AI Job", job["name"], field, "", update_modified=False)
    if jobs:
        frappe.db.commit()
    return {"jobs_scanned": len(jobs), "files_deleted": removed}


# A RUNNING job past this is dead: `frappe.enqueue(timeout=600)` hard-kills
# execution at 10 minutes, so past 30 the worker can no longer flip it to
# failed and the user's credits would be stranded forever.
_STALE_RUNNING_MINUTES = 30

# A QUEUED job is a different animal. `timeout` bounds EXECUTION, not queue
# wait, so a job can sit queued for hours behind a backlog and still be
# perfectly alive. Failing and refunding it at 30 minutes meant the user was
# refunded, told it failed, re-submitted at their own cost — and then the
# original job dequeued, ran, and delivered anyway. Free vendor spend, at
# exactly the moment vendor spend is already peaking. Give the queue a full
# working day before declaring anything in it dead.
_STALE_QUEUED_HOURS = 12

# Kept for backwards compatibility with anything importing the old name.
_STALE_JOB_MINUTES = _STALE_RUNNING_MINUTES


def cleanup_stale_ai_jobs():
    """Scheduled hourly (hooks.py): fail out AI Jobs stuck in queued/running.

    An RQ timeout or a SIGKILL'ed worker leaves the job doc non-terminal
    forever — run_job's exception handling never fires, so the refund path
    never runs and the ops report undercounts failures. Mark anything stale
    as failed with a user-safe message and run the standard idempotent
    refund. Deliberately does NOT feed the circuit breaker: a stale job says
    nothing about *current* vendor health.

    `running` and `queued` get different deadlines — see the constants above.
    Conflating them refunded live work."""
    from frappe.utils import add_to_date, now_datetime

    now = now_datetime()
    running_cutoff = add_to_date(now, minutes=-_STALE_RUNNING_MINUTES)
    queued_cutoff = add_to_date(now, hours=-_STALE_QUEUED_HOURS)
    rows = frappe.get_all(
        "AI Job",
        or_filters=[
            {"status": "running", "modified": ["<", running_cutoff]},
            {"status": "queued", "modified": ["<", queued_cutoff]},
        ],
        pluck="name",
        limit_page_length=200,
    )
    failed_out = 0
    for name in rows:
        try:
            doc = frappe.get_doc("AI Job", name)
            if doc.status not in ("queued", "running"):
                continue
            doc.status = "failed"
            doc.error_code = "WORKER_ERROR"
            doc.error_message = (
                "This job was interrupted on our side. "
                "Your credits have been refunded — please try again."
            )
            doc.completed_at = now_datetime()
            doc.save(ignore_permissions=True)
            _refund_job_credits(
                doc.user,
                doc.name,
                doc.idempotency_key,
                reason="AI job stale/interrupted (watchdog)",
            )
            failed_out += 1
        except Exception:
            frappe.log_error(frappe.get_traceback(), "v1.cleanup_stale_ai_jobs")
    if failed_out:
        frappe.db.commit()
    return {"jobs_failed_out": failed_out}


@frappe.whitelist(methods=["POST"])
def delete_my_ai_photos(**kwargs):
    """User-initiated purge of every photo the caller ever uploaded to the AI
    tools (E1) — source, mask, and reference files on their AI Jobs, plus the
    source files retained by their saved designs. Generated results are kept."""
    try:
        _require_auth()
    except frappe.AuthenticationError:
        return _error_response("UNAUTHORIZED", "Authentication required", 401)

    user = frappe.session.user
    removed = 0

    jobs = frappe.get_all(
        "AI Job",
        filters={"user": user},
        fields=["name", "source_image_url", "mask_url", "ref_image_url"],
        limit_page_length=0,
    )
    for job in jobs:
        for field in ("source_image_url", "mask_url", "ref_image_url"):
            url = job.get(field)
            if url:
                removed += _delete_private_file(url)
                frappe.db.set_value("AI Job", job["name"], field, "", update_modified=False)

    designs = frappe.get_all(
        "AI Design",
        filters={"user": user},
        fields=["name", "source_file"],
        limit_page_length=0,
    )
    for design in designs:
        url = design.get("source_file")
        if url:
            removed += _delete_private_file(url)
            frappe.db.set_value("AI Design", design["name"], "source_file", "", update_modified=False)

    frappe.db.commit()
    return {"files_deleted": removed}


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


# C7: tools whose per-unit rate is honest without knowing the room's area —
# the unit itself is per sq ft, so area cancels out. Everything else returns
# an empty hint list (fabricating a total for a photo would undermine the
# estimator's credibility).
_COST_HINT_TOOLS = {"floor", "paint"}


@frappe.whitelist()
def ai_cost_hint(tool_id=None, country=None):
    """C7: per-unit material rate tags for an image-edit result.

    Reads the same Construction Setting rate table the PK estimator uses, so
    the numbers on the AI result screen and in a full estimate can never
    disagree. Rates left at 0/unset are omitted; an empty `hints` list tells
    the client to hide the tag entirely.

    The rates are Pakistan-market (Construction Setting is the PK table) —
    the note labels them as such, and a client that knows its user is in
    another market can pass `country` to suppress the tag entirely.
    """
    try:
        _require_auth()
    except frappe.AuthenticationError:
        return _error_response("UNAUTHORIZED", "Authentication required", 401)

    tool_id = _resolve_tool_id(tool_id)
    empty = {"tool_id": tool_id, "currency": "PKR", "unit": "sq ft", "hints": []}
    if tool_id not in _COST_HINT_TOOLS:
        return empty
    if country and str(country).strip().upper() not in ("PK", "PAKISTAN"):
        return empty
    try:
        settings = frappe.get_single("Construction Setting")
    except Exception:
        return empty

    hints = []

    def _add(label, rate):
        try:
            rate = float(rate or 0)
        except (TypeError, ValueError):
            return
        if rate > 0:
            hints.append({"label": label, "rate": round(rate, 2)})

    if tool_id == "floor":
        _add("Marble", getattr(settings, "marble_rate", 0))
        _add("Tile", getattr(settings, "tile_rate", 0))
        _add("Laying labour", getattr(settings, "floor_labour_rate", 0))
    elif tool_id == "paint":
        _add("Paint incl. labour", getattr(settings, "paint_with_lab_rate", 0))
        _add("Simple paint incl. labour",
             getattr(settings, "simple_paint_with_lab_rate", 0))

    return {
        "tool_id": tool_id,
        "currency": "PKR",
        "unit": "sq ft",
        "hints": hints,
        "note": "Pakistan market rates from your estimator — not a full quote.",
    }


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
    """REMOVED — Pro is paid-only, there is no free trial (product decision,
    July 2026). Kept as a stub so old clients get a clean structured error
    instead of a 404, and so the route can never silently grant premium again.
    """
    return _error_response(
        "TRIAL_UNAVAILABLE",
        "Free trials are no longer offered. Subscribe to unlock Pro.",
        410,
    )


# Play Console subscription products. Mirror of the client's
# IapProductIds.subscriptions — the server never trusts the client's claimed
# product id (Play's line items are the truth), this set just bounds what we
# accept at all.
_SUBSCRIPTION_PRODUCTS = {
    "buildcost_pro_monthly_v1",
    "buildcost_pro_annual_v1",
}


def _premium_table_lock(timeout=15):
    """`AI Settings.premium_users` is a child table on a singleton, and every
    writer is a read-modify-write of the WHOLE table. Two activations landing
    together therefore each save their own copy and the later save silently
    drops the earlier one's row — a paying customer loses their entitlement
    with nothing logged. Serialise the read-modify-write."""
    return _user_ledger_lock("ai-settings-premium-users", timeout=timeout)


def _premium_rows(
    user=None, purchase_token=None, extra_filters=None, order_by=None, limit=0
):
    """Premium entries, optionally filtered. Read straight from the child
    table so callers don't have to load and walk the singleton.

    `extra_filters`, `order_by` and `limit` exist so a caller that can only
    afford to look at N rows can push the selection into SQL. Slicing the
    full result in Python instead meant revoked and lapsed rows consumed the
    budget before any live one was reached.
    """
    filters = {
        "parent": "AI Settings",
        "parenttype": "AI Settings",
        "parentfield": "premium_users",
    }
    if user:
        filters["user"] = user
    if purchase_token:
        filters["purchase_token"] = purchase_token
    if extra_filters:
        filters.update(extra_filters)
    return frappe.get_all(
        "User Premium Entry",
        filters=filters,
        fields=[
            "name", "user", "expires_at", "note", "platform",
            "purchase_token", "last_verified_at", "revoked_at",
        ],
        order_by=order_by,
        limit_page_length=int(limit or 0),
    )


def _upsert_premium_row(
    user, product_id, expires, purchase_token=None, platform="android",
    linked_purchase_token=None,
):
    """Insert or update the (user, product_id) premium entry.

    MUST be called under `_premium_table_lock()`.

    Also records the purchase token. Without it nothing downstream can ever
    revalidate or revoke the entitlement — a refunded annual subscription just
    keeps working for the remaining 363 days.
    """
    from frappe.utils import now_datetime

    settings = frappe.get_doc("AI Settings")
    found = False
    for row in (settings.get("premium_users") or []):
        if row.user == user and (row.note or "") == product_id:
            row.expires_at = expires
            row.purchase_token = purchase_token or row.purchase_token
            row.platform = platform or row.platform
            row.last_verified_at = now_datetime()
            row.revoked_at = None
            found = True
            break
    if not found:
        settings.append("premium_users", {
            "user": user,
            "expires_at": expires,
            "note": product_id,
            "purchase_token": purchase_token,
            "platform": platform,
            "last_verified_at": now_datetime(),
        })

    # F-41: an upgrade/downgrade issues a NEW token and reports the one it
    # replaced as `linkedPurchaseToken`. The old row was being left in place,
    # so the superseded plan kept granting until its own expiry ran out.
    if linked_purchase_token and linked_purchase_token != purchase_token:
        settings.premium_users = [
            row for row in (settings.get("premium_users") or [])
            if (row.purchase_token or "") != linked_purchase_token
        ]
        for idx, row in enumerate(settings.premium_users, start=1):
            row.idx = idx

    settings.save(ignore_permissions=True)
    frappe.clear_document_cache("AI Settings", "AI Settings")


@frappe.whitelist(methods=["POST"])
def activate_premium(**kwargs):
    """Activate or extend premium for the caller from a Play subscription
    purchase token.

    Body (JSON):
        platform:   "android" (iOS not supported yet)
        product_id: client-claimed product id (bounds-checked only; Play's
                    line items are authoritative)
        receipt:    the Play purchase token
                    (PurchaseDetails.verificationData.serverVerificationData)

    The expiry is ALWAYS derived server-side from the Play Developer API —
    any client-sent `expires_at` is ignored. Fail-closed: when validation
    isn't configured (see play_billing.py) this refuses with 503 rather than
    trusting the client. There is no relaxed/QA bypass.

    Successful activation also seeds the current month's Pro credit grant
    (idempotent per user+period), so a fresh subscriber can generate
    immediately; renewals are topped up by the daily scheduler task.
    """
    try:
        _require_auth()
    except frappe.AuthenticationError:
        return _error_response("UNAUTHORIZED", "Authentication required", 401)
    from construction.api import play_billing
    from frappe.utils import now_datetime

    body = _read_json_body() or {}
    receipt = (body.get("receipt") or "").strip()
    claimed_product = (body.get("product_id") or "").strip()
    if not receipt:
        return _error_response("MISSING_PARAMS", "receipt is required", 400)
    if claimed_product and claimed_product not in _SUBSCRIPTION_PRODUCTS:
        return _error_response(
            "UNKNOWN_PRODUCT",
            f"Subscription product {claimed_product!r} is not recognised",
            400,
        )

    user = frappe.session.user
    linked_token = None

    play_billing.warn_if_relaxed_configured()

    try:
        verified = play_billing.verify_subscription(receipt)
    except play_billing.PlayBillingError as e:
        return _error_response(e.code, e.message, e.http_status)
    product_id = verified["product_id"] or claimed_product or "premium"
    expires = verified["expires_at"]
    linked_token = verified.get("linked_purchase_token")
    if not verified["entitled"] or not expires or expires <= now_datetime():
        return _error_response(
            "RECEIPT_NOT_ACTIVE",
            "This subscription is not active according to Google Play.",
            400,
            details={"state": verified["state"]},
        )

    # F-17: bind the receipt to the caller. Play already tells us who the
    # purchase belongs to and the answer was being read and thrown away, so
    # one valid token activated Pro on as many accounts as it was replayed
    # against — each then drawing 80 credits a month.
    claim_err = _reject_foreign_receipt(user, receipt, verified.get("obfuscated_account_id"))
    if claim_err is not None:
        return claim_err

    try:
        with _premium_table_lock():
            _upsert_premium_row(
                user, product_id, expires,
                purchase_token=receipt,
                platform=(body.get("platform") or "android")[:16],
                linked_purchase_token=linked_token,
            )
    except LedgerLockError:
        return _error_response(
            "BUSY", "Please try again in a moment.", 409,
        )

    # Seed this billing period's Pro credits so the subscriber isn't stuck on
    # leftover welcome credits until the nightly scheduler runs. Idempotent per
    # (user, period), so activate + scheduler can never double-grant.
    #
    # F-24: this used to be best-effort — a failure here left the customer
    # entitled but with zero credits, i.e. paid and immediately 402'd. The
    # grant is retried once and its outcome is reported, and the daily task is
    # the backstop if both attempts fail.
    credits_granted = False
    for attempt in (1, 2):
        try:
            _grant_monthly_credits(user, reason=f"Pro activation: {product_id}")
            credits_granted = True
            break
        except Exception:
            frappe.log_error(
                frappe.get_traceback(), f"activate_premium.monthly_grant.{attempt}"
            )
    frappe.db.commit()
    state = _get_credit_state(user)
    return {
        "premium": True,
        "expires_at": str(expires),
        "source": product_id,
        "credits_granted": credits_granted,
        "balance": state["total_balance"],
    }


def _account_binding_id(user):
    """The value the client should send Play as `obfuscatedAccountId`.

    A hash, not the account id itself: Play stores it, shows it in the
    Console, and it must not be reusable as a credential or leak an email.
    """
    return hashlib.sha256(f"acct:{user}".encode("utf-8")).hexdigest()[:64]


def _reject_foreign_receipt(user, purchase_token, obfuscated_account_id):
    """Refuse a purchase token that already belongs to a different account.

    Two independent checks, because they cover different eras:

    * `obfuscatedExternalAccountId` — set by the client at purchase time and
      returned by Play. When it is present and in our own format it is
      authoritative. Purchases made before the app started sending it (or by a
      client that omits it) simply don't have one.
    * token exclusivity — whoever claims a token first owns it. This holds for
      legacy purchases with no account id, and is the check that actually
      stops a token being replayed across accounts.

    Returns an error response, or None when the claim is legitimate.
    """
    expected = _account_binding_id(user)
    if obfuscated_account_id:
        oid = str(obfuscated_account_id).strip()
        # Only enforce against ids in OUR format; a legacy client sent its
        # device id here, and rejecting those would strand real subscribers.
        if len(oid) == 64 and all(c in "0123456789abcdef" for c in oid.lower()):
            if oid.lower() != expected:
                frappe.log_error(
                    f"activate_premium: receipt bound to another account "
                    f"(caller={user})",
                    "v1.activate_premium.foreign_receipt",
                )
                return _error_response(
                    "RECEIPT_FOREIGN",
                    "This purchase belongs to a different account.",
                    403,
                )
            return None

    for row in _premium_rows(purchase_token=purchase_token):
        if row.user != user:
            frappe.log_error(
                f"activate_premium: token already claimed by {row.user}, "
                f"replayed by {user}",
                "v1.activate_premium.foreign_receipt",
            )
            return _error_response(
                "RECEIPT_FOREIGN",
                "This purchase belongs to a different account.",
                403,
            )
    return None


def _clawback_credits(user, purchase_token, reason, state=None, period=None):
    """Write the `clawback_refund` event the ledger schema has always defined
    and nothing has ever written.

    Removes the granted credits the revoked purchase paid for, down to zero —
    we claw back what is left, not what was spent, because a negative balance
    from a refund would lock a user out of the free tier too.

    `state` and `period` MUST be the ones captured while the entitlement was
    still live (see `_revoke_premium_row`). Reading them here instead meant
    reading them *after* `expires_at` had been set to now: the user had
    already fallen back to a calendar period, where the monthly bucket reads
    empty because the Pro grant lives under the billing-anchor label. The
    clawback then took the whole amount out of `topup` — credits bought in a
    separate transaction — and left the grant it was aimed at untouched.

    Only the monthly bucket is subject to clawback. Topup credits are a
    distinct purchase; voiding a subscription is not grounds to confiscate
    them, and if that pack was itself refunded Play sends its own voided
    notification for that token.
    """
    state = state or _get_credit_state(user)
    period = period or _current_period_month(user)
    take = min(max(0, state["monthly_balance"]), _DEFAULT_MONTHLY_QUOTA)
    if take <= 0:
        return 0
    _insert_ledger_event(
        user,
        event_type="clawback_refund",
        bucket="monthly",
        delta=-take,
        reason=reason[:200],
        idempotency_key=f"{_idem('clawback', user, purchase_token)}:m",
        period_month=period,
    )
    return take


def _revoke_premium_row(row_name, user, purchase_token, reason):
    """Mark one entitlement revoked and claw back its credits. Idempotent."""
    from frappe.utils import now_datetime

    # Captured BEFORE the row is expired, because both values are derived from
    # the live entitlement — see `_clawback_credits`.
    try:
        period = _current_period_month(user)
        state = _get_credit_state(user)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "v1.revoke_premium.state")
        period, state = None, None

    frappe.db.set_value(
        "User Premium Entry",
        row_name,
        {"expires_at": now_datetime(), "revoked_at": now_datetime()},
        update_modified=False,
    )
    frappe.clear_document_cache("AI Settings", "AI Settings")
    if state is not None:
        try:
            # The ledger write is a read-decide-insert like every other, and
            # this one runs from a scheduler and a webhook, i.e. concurrently
            # with the user's own submits.
            with _user_ledger_lock(user):
                _clawback_credits(
                    user, purchase_token, reason, state=state, period=period
                )
        except LedgerLockError:
            # The entitlement is revoked either way; only the credit clawback
            # is lost, and nothing retries it (revalidation skips revoked
            # rows). Loud enough to reconcile by hand.
            frappe.log_error(
                f"Premium revoked for {user} but the credit clawback could "
                f"not run: ledger busy. Reconcile manually.",
                "v1.revoke_premium.clawback_busy",
            )
        except Exception:
            frappe.log_error(frappe.get_traceback(), "v1.revoke_premium.clawback")
    # Not an error — a revocation is an ordinary lifecycle event — but it moves
    # money, so it belongs in a log somebody actually reads.
    frappe.logger().info(f"Premium revoked for {user}: {reason}")


def revalidate_premium_subscriptions(limit=200):
    """Daily scheduler task: re-check live entitlements against Play.

    Money moved out of this system and never back: there was no RTDN handler,
    no Voided Purchases polling, no revalidation, and the `clawback_refund`
    event the schema defines had zero writers. A refunded annual subscription
    kept Pro for the remaining ~363 days and collected twelve further monthly
    credit grants.

    This is the polling half (the push half is `play_rtdn`). It is deliberately
    the backstop rather than the primary: RTDN can be missed, misconfigured, or
    silently unsubscribed, and a daily sweep notices anyway.
    """
    from construction.api import play_billing
    from frappe.utils import now_datetime

    if not play_billing.is_configured():
        return {"checked": 0, "revoked": 0, "skipped": "play_billing_unconfigured"}

    now = now_datetime()
    checked = 0
    revoked = 0
    # Select in SQL, not in Python. `_premium_rows()[:limit]` took the first N
    # rows of the whole table in insertion order and only then skipped the
    # revoked and lapsed ones, so past N entries the live tail was never
    # revalidated at all — and it was the same N rows every single day.
    # Oldest check first (MariaDB sorts NULL first on ASC, so rows that have
    # never been verified lead), which makes the cap a rotation rather than a
    # cut-off.
    rows = _premium_rows(
        extra_filters={
            "revoked_at": ["is", "not set"],
            "purchase_token": ["is", "set"],
            "expires_at": [">", now],
        },
        order_by="last_verified_at asc",
        limit=int(limit),
    )
    for row in rows:
        checked += 1
        try:
            verified = play_billing.verify_subscription(row.purchase_token)
        except play_billing.PlayBillingError as e:
            if e.code == "INVALID_RECEIPT":
                _revoke_premium_row(
                    row.name, row.user, row.purchase_token,
                    "Play no longer recognises this purchase token",
                )
                revoked += 1
            # PLAY_API_ERROR is transient — leave the entitlement alone and
            # let tomorrow's run decide.
            continue
        except Exception:
            frappe.log_error(frappe.get_traceback(), "v1.revalidate_premium")
            continue

        entitled = verified.get("entitled") and verified.get("expires_at")
        if not entitled:
            _revoke_premium_row(
                row.name, row.user, row.purchase_token,
                f"Play reports state={verified.get('state')}",
            )
            revoked += 1
            continue

        # Still good — carry Play's expiry forward so a renewal lands without
        # the user having to relaunch the app, and stamp the check.
        frappe.db.set_value(
            "User Premium Entry",
            row.name,
            {"expires_at": verified["expires_at"], "last_verified_at": now},
            update_modified=False,
        )
    frappe.clear_document_cache("AI Settings", "AI Settings")
    frappe.db.commit()
    return {"checked": checked, "revoked": revoked}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def play_rtdn(**kwargs):
    """Google Play Real-Time Developer Notifications push endpoint.

    Subscribe this URL as a Pub/Sub *push* endpoint with authentication
    enabled, and set `play_rtdn_audience` (the push config's audience) plus
    `play_rtdn_service_account` (the service account Pub/Sub signs as) in
    site_config. The OIDC token in the Authorization header is verified before
    a single byte of the body is trusted — an unauthenticated endpoint that
    revokes entitlements is its own denial-of-service.

    We do not trust the notification's contents to decide anything: it only
    tells us WHICH token to re-check. The decision always comes from a fresh
    `verify_subscription` call.
    """
    auth_err = _verify_rtdn_token()
    if auth_err is not None:
        return auth_err

    import base64

    body = _read_json_body() or {}
    msg = (body.get("message") or {}) if isinstance(body, dict) else {}
    raw = msg.get("data")
    if not raw:
        return {"ok": True, "ignored": "no data"}
    try:
        payload = json.loads(base64.b64decode(raw).decode("utf-8"))
    except Exception:
        return {"ok": True, "ignored": "undecodable"}

    sub = payload.get("subscriptionNotification") or {}
    voided = payload.get("voidedPurchaseNotification") or {}
    token = sub.get("purchaseToken") or voided.get("purchaseToken")
    if not token:
        return {"ok": True, "ignored": "no token"}

    from construction.api import play_billing
    from frappe.utils import now_datetime

    rows = _premium_rows(purchase_token=token)
    if not rows:
        return {"ok": True, "ignored": "unknown token"}

    handled = 0
    for row in rows:
        if row.revoked_at:
            continue
        if voided:
            _revoke_premium_row(row.name, row.user, token, "Play voided this purchase")
            handled += 1
            continue
        try:
            verified = play_billing.verify_subscription(token)
        except Exception:
            # Can't confirm right now; the daily sweep will.
            continue
        if verified.get("entitled") and verified.get("expires_at"):
            frappe.db.set_value(
                "User Premium Entry",
                row.name,
                {"expires_at": verified["expires_at"], "last_verified_at": now_datetime()},
                update_modified=False,
            )
        else:
            _revoke_premium_row(
                row.name, row.user, token,
                f"RTDN + Play state={verified.get('state')}",
            )
        handled += 1
    frappe.clear_document_cache("AI Settings", "AI Settings")
    frappe.db.commit()
    return {"ok": True, "handled": handled}


def _verify_rtdn_token():
    """Verify the Pub/Sub push OIDC token. Returns an error response or None."""
    conf = frappe.get_site_config()
    audience = conf.get("play_rtdn_audience")
    expected_sa = conf.get("play_rtdn_service_account")
    if not audience or not expected_sa:
        # Unconfigured means unsubscribed. Refuse rather than accept anonymous
        # revocation requests.
        return _error_response(
            "RTDN_UNCONFIGURED", "This endpoint is not configured.", 503
        )
    header = ""
    if frappe.request and frappe.request.headers:
        header = frappe.request.headers.get("Authorization") or ""
    if not header.lower().startswith("bearer "):
        return _error_response("UNAUTHORIZED", "Missing bearer token", 401)
    token = header.split(" ", 1)[1].strip()
    try:
        from google.auth.transport import requests as ga_requests
        from google.oauth2 import id_token

        claims = id_token.verify_oauth2_token(
            token, ga_requests.Request(), audience
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "v1.play_rtdn.bad_token")
        return _error_response("UNAUTHORIZED", "Invalid token", 401)
    if claims.get("email") != expected_sa or not claims.get("email_verified"):
        return _error_response("UNAUTHORIZED", "Unexpected caller", 403)
    return None


def grant_monthly_credits_for_active_pros():
    """Daily scheduler task (hooks.py): top up the monthly credit bucket for
    every user with an active, non-trial premium entry. Idempotent per
    (user, period) via `_grant_monthly_credits`, so running daily just makes
    each user's grant land on the first run of their billing month.
    """
    from frappe.utils import now_datetime

    settings = ai_cfg.get_settings()
    now = now_datetime()
    seen = set()
    for row in (settings.get("premium_users") or []):
        if row.user in seen:
            continue
        if (row.note or "") == "trial":
            continue
        if getattr(row, "revoked_at", None):
            continue
        if row.expires_at and row.expires_at <= now:
            continue
        seen.add(row.user)
        try:
            _grant_monthly_credits(row.user, reason=f"Monthly Pro grant ({row.note or 'manual'})")
        except Exception:
            frappe.log_error(frappe.get_traceback(), "grant_monthly_credits_for_active_pros")
    if seen:
        frappe.db.commit()


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

    Unauthenticated by necessity — a crash report is most valuable exactly when
    auth is what broke — so it is bounded instead: a per-IP daily cap, and hard
    length limits on every field. Without those it is an open, unbounded write
    into the table the team reads to triage real incidents, which makes both
    flooding it and hiding a real crash in the noise trivial.
    """
    try:
        if not _ip_daily_allowed("client_err_ip", _CLIENT_ERROR_IP_DAILY_CAP):
            # Deliberately a success shape: a client that can't report an error
            # should not then error about failing to report it.
            return {"ok": True, "correlation_id": None, "throttled": True}

        data = _read_json_body()
        if not isinstance(data, dict):
            data = {}

        def _cap(key, limit):
            val = data.get(key)
            if val is None:
                return None
            return str(val)[:limit]

        correlation_id = (str(data.get("correlation_id") or "")[:64]
                          or uuid.uuid4().hex[:12])

        from construction.construction.doctype.app_error_log.app_error_log import (
            AppErrorLog,
        )
        source = data.get("source") or "app"
        AppErrorLog.record(
            source=source if source in ("app", "api") else "app",
            level=_cap("level", 20),
            is_fatal=bool(data.get("is_fatal")),
            title=_cap("title", 140),
            message=(_cap("message", 2000) or _cap("stack", 2000)
                     or "Unknown client error"),
            error_type=_cap("error_type", 140),
            # Accept both `stack` (legacy) and `stack_trace`.
            stack_trace=(_cap("stack_trace", 8000) or _cap("stack", 8000)),
            endpoint=_cap("endpoint", 255),
            http_method=_cap("http_method", 10),
            http_status=data.get("http_status"),
            request_id=_cap("request_id", 64),
            route=_cap("route", 255),
            correlation_id=correlation_id,
            network_status=_cap("network_status", 40),
            breadcrumbs=_cap("breadcrumbs", 4000),
            context_json=_cap("context_json", 8000),
            device_model=_cap("device_model", 140),
            os_version=_cap("os_version", 60),
            # F-29: the raw device id is no longer accepted here. It used to be
            # written verbatim into a table anyone can read a copy of, while
            # simultaneously being the thing that unlocked an account.
            device_id=(hashlib.sha256(str(data.get("device_id")).encode("utf-8")).hexdigest()[:32]
                       if data.get("device_id") else None),
            app_version=_cap("app_version", 40),
            build_number=_cap("build_number", 40),
            platform=_cap("platform", 20),
            environment=_cap("environment", 20),
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

    gate = _require_sync_entitlement()
    if gate is not None:
        return gate

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
            "client_id", "project_name", "location", "budget", "currency",
            "estimate_client_id", "client_created_at", "updated_at",
            "updated_at_utc", "archived", "deleted",
        ],
        order_by="updated_at desc",
        limit_page_length=0,
    )
    server_expenses = frappe.get_all(
        "Expense Entry",
        filters={"user": user},
        fields=[
            "client_id", "project_client_id", "category", "custom_name",
            "material", "qty", "uom", "amount", "note", "vendor",
            "payment_method", "entry_date",
            "client_created_at", "updated_at", "updated_at_utc", "deleted",
        ],
        order_by="updated_at desc",
        limit_page_length=0,
    )

    for r in server_projects:
        r["created_at"] = str(r.pop("client_created_at")) if r.get("client_created_at") else None
        r["updated_at"] = str(r["updated_at"]) if r.get("updated_at") else None
        r["name"] = r.pop("project_name") or ""
        r["estimate_id"] = r.pop("estimate_client_id") or None
        r["updated_at_utc"] = str(r["updated_at_utc"]) if r.get("updated_at_utc") else None
        r["archived"] = bool(r.get("archived"))
        r["deleted"] = bool(r.get("deleted"))

    for r in server_expenses:
        r["created_at"] = str(r.pop("client_created_at")) if r.get("client_created_at") else None
        r["updated_at"] = str(r["updated_at"]) if r.get("updated_at") else None
        r["date"] = str(r.pop("entry_date")) if r.get("entry_date") else None
        r["updated_at_utc"] = str(r["updated_at_utc"]) if r.get("updated_at_utc") else None
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
            incoming_utc = _parse_utc(row.get("updated_at_utc"))
            row["_utc_norm"] = incoming_utc
            if existing_name:
                server_updated, server_utc = frappe.db.get_value(
                    doctype, existing_name, ["updated_at", "updated_at_utc"]
                )
                if server_utc and incoming_utc:
                    # Both sides carry the epoch-exact UTC twin (E14) —
                    # timezone-safe comparison.
                    if incoming_utc <= server_utc:
                        continue
                elif (
                    server_updated
                    and incoming_updated
                    and incoming_updated <= server_updated
                ):
                    # Legacy naive-local compare for pre-E14 rows.
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
    doc.currency = (row.get("currency") or "PKR")[:8]
    doc.estimate_client_id = row.get("estimate_id") or ""
    doc.client_created_at = (
        _parse_dt(row.get("created_at")) or frappe.utils.now_datetime()
    )
    doc.updated_at = (
        _parse_dt(row.get("updated_at")) or frappe.utils.now_datetime()
    )
    doc.updated_at_utc = row.get("_utc_norm")
    doc.archived = 1 if row.get("archived") else 0
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
    doc.vendor = (row.get("vendor") or "")[:160]
    doc.payment_method = (row.get("payment_method") or "")[:20]
    doc.entry_date = _parse_dt(row.get("date"))
    doc.client_created_at = (
        _parse_dt(row.get("created_at")) or frappe.utils.now_datetime()
    )
    doc.updated_at = (
        _parse_dt(row.get("updated_at")) or frappe.utils.now_datetime()
    )
    doc.updated_at_utc = row.get("_utc_norm")
    doc.deleted = 1 if row.get("deleted") else 0


def _parse_utc(value):
    """Parse an ISO-8601 instant into naive-UTC. Offset-aware input is
    converted to UTC then stripped; naive input is assumed to already be UTC
    (the client only sends naive-UTC in updated_at_utc). Absurdly-future
    stamps (> 1 day ahead) are clamped to now so a device with a wrong clock
    can't permanently win last-write-wins (E6). Returns None on garbage."""
    if not value:
        return None
    from datetime import datetime, timedelta, timezone as _tz
    try:
        from dateutil import parser as _du
        dt = _du.isoparse(str(value))
    except Exception:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(_tz.utc).replace(tzinfo=None)
    now = datetime.utcnow()
    if dt > now + timedelta(days=1):
        dt = now
    return dt


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

# `AI Credit Ledger.idempotency_key` is Data(80) with a unique index.
_LEDGER_KEY_MAX = 80


def _idem(*parts):
    """Build a fixed-width, wildcard-free ledger idempotency key.

    Every key in this ledger is derived from client-supplied text — the
    `Idempotency-Key` header, a device id, a user's email. Interpolating that
    text into a key raw had two consequences, and both were exploitable:

    * the value reached a SQL ``LIKE`` pattern, so a key of ``%`` matched every
      prior debit row and the server concluded the run was already paid for;
    * long values overflowed the 80-char column, so a compensating reversal
      inserted as a *fresh* credit instead of deduplicating against itself.

    Hashing closes both at once. The first part stays in the clear as a
    readable namespace, so the ledger is still greppable by event kind;
    everything after it is folded into a digest. The result is at most 65
    chars and contains only ``[a-z0-9_:]`` — no SQL metacharacters, no
    overflow, and still deterministic for the same inputs.
    """
    prefix = re.sub(r"[^a-z0-9_]", "", str(parts[0] or "key").lower())[:24] or "key"
    joined = "\x1f".join("" if p is None else str(p) for p in parts)
    return f"{prefix}:{hashlib.sha256(joined.encode('utf-8')).hexdigest()[:40]}"


def _debit_sub_keys(base_key):
    """The two per-bucket keys one debit can produce. Short suffixes on
    purpose: `_idem` already spends 65 of the column's 80 chars."""
    if not base_key:
        return None, None
    return f"{base_key}:m", f"{base_key}:t"


class LedgerLockError(Exception):
    """Raised when the per-user credit lock could not be acquired."""


@contextmanager
def _user_ledger_lock(user, timeout=10):
    """Serialise every credit mutation for one user, across processes.

    The spend decision is read-balance -> decide -> insert, which is not
    atomic: two concurrent submits both read the same balance, both conclude
    there is room, and both insert. One generation is then free. A MariaDB
    advisory lock keyed on the user makes that whole sequence mutually
    exclusive across web workers and background jobs.

    Fails CLOSED — if the lock can't be taken we raise rather than fall
    through to the unserialised path, because falling through is exactly the
    race being closed. The lock is connection-scoped, so it survives the
    commits taken inside the block, and it is always released in `finally`.
    """
    key = "credits:" + hashlib.sha256((user or "").encode("utf-8")).hexdigest()[:48]

    # Re-entrant. The refund helpers take this lock themselves so the worker
    # and the watchdog are covered, but `_ai_submit` already holds it when it
    # compensates a failed enqueue. Tracking what this connection holds keeps
    # the inner acquisition a no-op instead of relying on MariaDB's recursive
    # GET_LOCK counter and a matching number of releases.
    held = getattr(frappe.local, "_ledger_locks_held", None)
    if held is None:
        held = set()
        frappe.local._ledger_locks_held = held
    if key in held:
        yield
        return

    try:
        got = frappe.db.sql("select get_lock(%s, %s)", (key, int(timeout)))
    except Exception:
        frappe.log_error(frappe.get_traceback(), "v1.ledger_lock_acquire")
        raise LedgerLockError("Could not acquire the credit lock")
    if not got or not got[0] or not got[0][0]:
        raise LedgerLockError("Timed out acquiring the credit lock")
    held.add(key)
    try:
        yield
    finally:
        held.discard(key)
        try:
            frappe.db.sql("select release_lock(%s)", (key,))
        except Exception:
            frappe.log_error(frappe.get_traceback(), "v1.ledger_lock_release")


def _calendar_period():
    """The current calendar month as 'YYYY-MM'. The period free users are on."""
    return frappe.utils.now_datetime().strftime("%Y-%m")


def _shift_month(d, months):
    """`d` moved by `months`, clamped to the last valid day of the target
    month (so an anchor of the 31st lands on the 30th / 28th where needed)."""
    import calendar
    from datetime import date

    total = (d.year * 12 + (d.month - 1)) + months
    year, month = divmod(total, 12)
    month += 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def _billing_period_start(anchor_day, today=None):
    """Start date of the billing period containing `today`, for a subscription
    whose renewal falls on `anchor_day` of the month.

    Every date built here is clamped to its own month's length. The previous
    version clamped the first construction and then rebuilt the fallback from
    the raw `anchor_day`, so an anchor of the 29th-31st raised `ValueError:
    day is out of range for month` for most of every short month. This sits
    under `_get_credit_state`, so that took out every balance read and every
    AI submit for the subscribers it hit.
    """
    import calendar
    from datetime import date

    def _on(year, month, day):
        return date(year, month, min(day, calendar.monthrange(year, month)[1]))

    today = today or frappe.utils.getdate(frappe.utils.now_datetime())
    anchor_day = int(anchor_day)
    start = _on(today.year, today.month, anchor_day)
    if start > today:
        # Still before this month's renewal, so we are in the period that
        # opened on the previous month's anchor. Clamp against THAT month.
        prev_year, prev_month = (
            (today.year - 1, 12) if today.month == 1
            else (today.year, today.month - 1)
        )
        start = _on(prev_year, prev_month, anchor_day)
    return start


def _subscription_anchor_day(user):
    """Day-of-month a Pro subscriber's credit period turns over, or None for
    free users. Taken from the entitlement's `expires_at`, which Play derives
    from the actual purchase date."""
    try:
        rows = frappe.get_all(
            "User Premium Entry",
            filters={
                "parent": "AI Settings",
                "parenttype": "AI Settings",
                "parentfield": "premium_users",
                "user": user,
            },
            fields=["expires_at", "revoked_at"],
            limit_page_length=0,
        )
    except Exception:
        return None
    now = frappe.utils.now_datetime()
    best = None
    for r in rows:
        if r.get("revoked_at"):
            continue
        exp = r.get("expires_at")
        if not exp:
            continue
        if exp <= now:
            continue
        if best is None or exp > best:
            best = exp
    if best is None:
        return None
    return frappe.utils.getdate(best).day


def _current_period_month(user=None):
    """The credit period `user` is currently in.

    Free users are on calendar months ('YYYY-MM'). Pro subscribers are on
    their OWN billing cycle, labelled by the period's start date
    ('YYYY-MM-DD').

    Granting per calendar month gave 13 grants per 12 payments: subscribe on
    the 31st and this month's 80 credits land immediately, next month's the
    following day. Anchoring the period on the subscription's renewal day
    makes one paid period yield exactly one grant.

    Called with no user for the handful of places that only need a coarse
    label (reversal tagging, ops reporting).
    """
    if not user or user == "Guest":
        return _calendar_period()
    anchor = _subscription_anchor_day(user)
    if not anchor:
        return _calendar_period()
    return _billing_period_start(anchor).strftime("%Y-%m-%d")


def _next_period_start(period_month=None, user=None):
    """First instant of the period after `period_month`. Used for
    `next_reset_at` in the API response."""
    period = period_month or _current_period_month(user)
    parts = [int(x) for x in period.split("-")]
    if len(parts) == 3:
        from datetime import date

        nxt = _shift_month(date(parts[0], parts[1], parts[2]), 1)
        return frappe.utils.get_datetime(f"{nxt} 00:00:00")
    year, month = parts
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
    """Computed credit state for `user`. Read-only; never mutates.

    Balances are deliberately NOT clamped at zero. Clamping hid overspend: a
    bucket that had gone negative read back as 0, so credits the user had
    already consumed were silently written off and the next debit started from
    a wrong baseline. A negative balance here is real — the spend gate
    compares against it and ops can see it in the ledger.
    """
    period = _current_period_month(user)
    monthly_balance = _sum_deltas(user, "monthly", period_month=period)
    topup_balance = _sum_deltas(user, "topup")
    monthly_used = max(0, monthly_quota - monthly_balance)
    return {
        "monthly_balance": monthly_balance,
        "monthly_quota": monthly_quota,
        "monthly_used": monthly_used,
        "topup_balance": topup_balance,
        "total_balance": monthly_balance + topup_balance,
        "period_month": period,
        "next_reset_at": str(_next_period_start(period, user=user)),
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
    legacy_keys=None,
):
    """Insert a single ledger row, returning the true balance afterwards.

    When `idempotency_key` is set and a row already exists with that key — or
    with any of `legacy_keys`, the pre-hash key formats rows may still carry
    from before `_idem` — this is a no-op and returns the existing row's
    `balance_after`. Callers rely on that for webhook and IAP retries.

    The pre-check races, so the column's unique index is treated as the real
    arbiter: a duplicate-key error on insert is the same no-op, not a 500.
    """
    lookup = [k for k in ([idempotency_key] + list(legacy_keys or [])) if k]
    if lookup:
        existing = frappe.db.get_value(
            "AI Credit Ledger",
            {"idempotency_key": ["in", lookup]},
            ["balance_after"],
            as_dict=True,
        )
        if existing:
            return int(existing.get("balance_after") or 0)

    if idempotency_key and len(idempotency_key) > _LEDGER_KEY_MAX:
        # Never silently truncate. A truncated key collides with every other
        # key sharing its prefix, which is how a compensating reversal once
        # inserted itself as a brand-new credit.
        raise ValueError(
            f"idempotency_key exceeds {_LEDGER_KEY_MAX} chars: {idempotency_key[:32]}..."
        )

    # Snapshot the balance *after* this event. Unclamped, matching
    # `_get_credit_state` — this column is an audit trail, not a display value.
    snapshot_total = _get_credit_state(user)["total_balance"] + int(delta)

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
    try:
        doc.insert(ignore_permissions=True)
    except (frappe.UniqueValidationError, frappe.DuplicateEntryError):
        # Lost the race against a concurrent identical event. That is the
        # dedup working, so report the winner's balance instead of failing.
        if not idempotency_key:
            raise
        existing = frappe.db.get_value(
            "AI Credit Ledger",
            {"idempotency_key": idempotency_key},
            ["balance_after"],
            as_dict=True,
        )
        return int((existing or {}).get("balance_after") or 0)
    return snapshot_total


def _grant_monthly_credits(user, period_month=None, quota=_DEFAULT_MONTHLY_QUOTA, reason=None):
    """Idempotent on (user, period_month). Resets monthly bucket to `quota`.

    No-rollover model: instead of accumulating, we reset by emitting a single
    grant for the target period. Re-running this for the same period is a
    no-op thanks to the idempotency key.
    """
    period = period_month or _current_period_month(user)
    legacy = f"grant_monthly:{user}:{period}"
    return _insert_ledger_event(
        user,
        event_type="grant_monthly",
        bucket="monthly",
        delta=int(quota),
        reason=reason or f"Monthly Pro grant for {period}",
        idempotency_key=_idem("grant_monthly", user, period),
        period_month=period,
        legacy_keys=[legacy, legacy[:_LEDGER_KEY_MAX]],
    )


def _grant_welcome_credits(user, amount=_DEFAULT_WELCOME_QUOTA, device_id=None, reason=None):
    """Idempotent per device — one welcome grant per device identity, ever.

    Keyed on the device id rather than the user so that deleting and
    re-bootstrapping an account cannot re-claim the grant.
    """
    legacy = f"welcome:{device_id}"
    return _insert_ledger_event(
        user,
        event_type="grant_welcome",
        bucket="topup",
        delta=int(amount),
        reason=reason or "Welcome design credits",
        idempotency_key=_idem("welcome", device_id),
        legacy_keys=[legacy, legacy[:_LEDGER_KEY_MAX]],
    )


# Per-IP daily ceiling on NEW welcome grants. This is a *secondary* speed bump
# against credit farming via rotating device_ids — NOT a real defense. The real
# fix is Play Integrity attestation on the device_id at bootstrap (needs client
# work); the hard money backstop is the global budget kill switch in
# `_check_budgets`. Kept generous because this is a mobile app: carrier-grade
# NAT can route many legitimate new installs through one IP, so a tight cap
# would deny real users their welcome credits. Fail-open everywhere.
_WELCOME_IP_DAILY_CAP = 50

# Ceiling on brand-new anonymous ACCOUNTS from one IP per day. Creation is
# unauthenticated by design (no-login product), so without this a script can
# mint accounts without limit and reset every per-user rate limit, free-quota
# counter and welcome grant simply by asking for a new identity. Deliberately
# looser than the welcome cap: carrier-grade NAT puts a lot of genuine first
# launches behind one address.
_BOOTSTRAP_IP_DAILY_CAP = 120

# Ceiling on client crash reports from one IP per day. Generous — a device in
# a crash loop legitimately produces a burst — but finite.
_CLIENT_ERROR_IP_DAILY_CAP = 500

# Ceiling on LEGACY ACCOUNT ADOPTIONS from one IP per day (see
# `anon_bootstrap`). Deliberately tight: a genuine device adopts exactly one
# account, exactly once. Anything working through a list of device ids is not
# a genuine device.
_ADOPT_IP_DAILY_CAP = 5


def _ip_daily_allowed(prefix, cap, ip=None):
    """Atomically claim one slot of a per-IP daily budget.

    Fails CLOSED. The previous version returned True on any error, which meant
    the one control standing between a script and unlimited free credits could
    be switched off by making the cache unavailable — and a get-then-set
    counter let concurrent callers walk straight through it regardless.
    """
    ip = ip or _client_ip()
    if not ip:
        return False
    from frappe.utils import nowdate

    n = _counter_incr(f"{prefix}:{ip}:{nowdate()}", 60 * 60 * 36)
    if n is None:
        return False
    return n <= cap


def _welcome_ip_allowed(ip=None):
    """True if this client IP hasn't exceeded the daily new-welcome-grant cap."""
    return _ip_daily_allowed("welcome_ip2", _WELCOME_IP_DAILY_CAP, ip=ip)


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

    CALLERS MUST HOLD `_user_ledger_lock(user)`. Everything below is a
    read-decide-insert sequence and is only atomic under that lock.
    """
    amount = int(amount)
    if amount <= 0:
        return {"applied": 0, "monthly_taken": 0, "topup_taken": 0, "balance_after": 0}

    monthly_key, topup_key = _debit_sub_keys(idempotency_key)

    # Idempotency check: look up the exact rows this key can have produced.
    # This used to be a `LIKE '<key>%'` pattern built from the raw client
    # header — a key of "%" matched every prior debit in the table and the
    # server concluded the run was already paid for. Exact match, always.
    if idempotency_key:
        prior = frappe.get_all(
            "AI Credit Ledger",
            filters={"idempotency_key": ["in", [monthly_key, topup_key]]},
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

    # Clamp at zero before splitting. `_get_credit_state` deliberately reports
    # buckets unclamped so overspend stays visible, but a NEGATIVE
    # `monthly_balance` here made `monthly_take` negative, which pushed
    # `topup_take = amount - monthly_take` ABOVE `amount` — the user was
    # silently charged the overdraft a second time out of their topup bucket,
    # while `applied` still summed to exactly `amount` so nothing surfaced.
    monthly_take = min(max(0, state["monthly_balance"]), amount)
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
            idempotency_key=monthly_key,
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
            idempotency_key=topup_key,
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
        fields=["delta", "bucket", "period_month"],
    )
    if not debits:
        return {"applied": 0, "balance_after": _get_credit_state(user)["total_balance"]}

    monthly_rows = [r for r in debits if r.bucket == "monthly" and int(r.delta) < 0]
    monthly_amt = -sum(int(r.delta) for r in monthly_rows)
    topup_amt = -sum(int(r.delta) for r in debits if r.bucket == "topup" and int(r.delta) < 0)
    base_key = _idem("refund", ref_doctype, ref_name)
    legacy_base = f"refund:{ref_doctype}:{ref_name}"
    monthly_key, topup_key = _debit_sub_keys(base_key)
    debit_period = next((r.period_month for r in monthly_rows if r.period_month), None)
    current_period = _current_period_month(user)
    balance_after = 0

    if monthly_amt > 0:
        if debit_period and debit_period != current_period:
            # The period those credits came from has already reset, so putting
            # them back there refunds into a dead bucket the user can never
            # spend. Land them in `topup` instead: the user genuinely gets the
            # credit back, and this month's quota is not inflated (which is
            # what tagging the refund with the *current* period used to do).
            balance_after = _insert_ledger_event(
                user,
                event_type="refund_failure",
                bucket="topup",
                delta=monthly_amt,
                reason=reason or "Refund on job failure (expired period)",
                ref_doctype=ref_doctype,
                ref_name=ref_name,
                idempotency_key=monthly_key,
                legacy_keys=[f"{legacy_base}:monthly"],
            )
        else:
            balance_after = _insert_ledger_event(
                user,
                event_type="refund_failure",
                bucket="monthly",
                delta=monthly_amt,
                reason=reason or "Refund on job failure",
                ref_doctype=ref_doctype,
                ref_name=ref_name,
                idempotency_key=monthly_key,
                period_month=debit_period or current_period,
                legacy_keys=[f"{legacy_base}:monthly"],
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
            idempotency_key=topup_key,
            legacy_keys=[f"{legacy_base}:topup"],
        )

    return {"applied": monthly_amt + topup_amt, "balance_after": balance_after}


def _job_debit_keys(user, job_idempotency_key):
    """The two ledger keys `_ai_submit` produced for this job's debit."""
    if not job_idempotency_key:
        return None, None
    return _debit_sub_keys(_idem("ai_submit", user, job_idempotency_key))


def _refund_job_credits(user, job_name, job_idempotency_key=None, reason=None):
    """Refund a job's debit, whether or not the `ref_name` backfill ever ran.

    `_refund_credits` finds the debit rows by `(ref_doctype, ref_name)`, and
    `ref_name` is written by an opportunistic UPDATE after the job insert whose
    failure was swallowed with a comment claiming refunds would still work "by
    job name". They would not — the rows the refund searches for are exactly
    the rows that UPDATE was supposed to label, so a silent backfill failure
    disabled refunds for that job permanently.

    Recomputing the debit key from (user, the job's own idempotency key) makes
    the refund independent of that write: if the labelled rows aren't there, we
    find them by key, label them, and refund normally.
    """
    # Read-decide-insert, same as the debit, and this one runs from the worker
    # and the watchdog — concurrently with the user's own submits by
    # definition. `_debit_credits` documents the requirement; the refund side
    # has to honour it too. Re-entrant, so `_ai_submit` compensating its own
    # failed enqueue while already holding the lock is fine.
    with _user_ledger_lock(user):
        found = frappe.db.get_value(
            "AI Credit Ledger",
            {
                "user": user,
                "ref_doctype": "AI Job",
                "ref_name": job_name,
                "event_type": "debit_submit",
            },
            "name",
        )
        if not found and job_idempotency_key:
            m_key, t_key = _job_debit_keys(user, job_idempotency_key)
            if m_key:
                frappe.db.sql(
                    """update `tabAI Credit Ledger`
                       set ref_name = %(name)s
                       where user = %(user)s
                         and ref_doctype = 'AI Job'
                         and (ref_name is null or ref_name = '')
                         and idempotency_key in (%(m_key)s, %(t_key)s)""",
                    {"name": job_name, "user": user, "m_key": m_key, "t_key": t_key},
                )
        return _refund_credits(user, "AI Job", job_name, reason=reason)


def _refund_partial_credits(
    user, ref_doctype, ref_name, delivered, paid_for, reason=None,
    job_idempotency_key=None,
):
    """Refund the share of a debit covering work that was never delivered.

    A four-variation run that returns two images was charged for four. The
    per-variation share of the original debit is returned to the topup bucket
    (which never expires), keyed idempotently on the job so a worker retry
    can't refund twice.

    Like `_refund_job_credits`, this recovers the debit rows from the job's
    own idempotency key when the opportunistic `ref_name` backfill never ran.
    Without that it found no debits, computed a charge of 0, and returned
    silently — the same swallowed-backfill failure mode that disabled full
    refunds, reintroduced on the partial path.
    """
    delivered = max(0, int(delivered))
    paid_for = max(1, int(paid_for))
    if delivered >= paid_for:
        return 0
    debit_filters = {
        "user": user,
        "ref_doctype": ref_doctype,
        "ref_name": ref_name,
        "event_type": "debit_submit",
    }
    debits = frappe.get_all("AI Credit Ledger", filters=debit_filters, fields=["delta"])
    if not debits and job_idempotency_key and ref_doctype == "AI Job":
        m_key, t_key = _job_debit_keys(user, job_idempotency_key)
        if m_key:
            frappe.db.sql(
                """update `tabAI Credit Ledger`
                   set ref_name = %(name)s
                   where user = %(user)s
                     and ref_doctype = 'AI Job'
                     and (ref_name is null or ref_name = '')
                     and idempotency_key in (%(m_key)s, %(t_key)s)""",
                {"name": ref_name, "user": user, "m_key": m_key, "t_key": t_key},
            )
            debits = frappe.get_all(
                "AI Credit Ledger", filters=debit_filters, fields=["delta"]
            )
    charged = -sum(int(r.delta) for r in debits if int(r.delta) < 0)
    if charged <= 0:
        return 0
    give_back = int(round(charged * (paid_for - delivered) / float(paid_for)))
    if give_back <= 0:
        return 0
    with _user_ledger_lock(user):
        _insert_ledger_event(
            user,
            event_type="refund_failure",
            bucket="topup",
            delta=give_back,
            reason=(reason or f"Partial delivery: {delivered}/{paid_for} variations")[:200],
            ref_doctype=ref_doctype,
            ref_name=ref_name,
            idempotency_key=_idem("refund_partial", ref_doctype, ref_name),
        )
    return give_back


def _reverse_debit(user, debit_key, debit_result):
    """Compensating reversal for a debit that succeeded but whose job failed to
    enqueue (so the ref_name backfill never ran and `_refund_credits`, which
    keys on ref, can't find it). Mirrors the per-bucket amounts back into the
    ledger. Idempotent on a derived reversal key.

    The suffixes are two characters because `_idem` already spends 65 of the
    `idempotency_key` column's 80: the old `:reversal:monthly` suffix pushed
    the key past the limit, and an overflowing key does not deduplicate — it
    inserts a fresh credit every time the path is hit.
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
            idempotency_key=f"{debit_key}:rm",
            period_month=_current_period_month(user),
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
            idempotency_key=f"{debit_key}:rt",
        )


# ===========================================================================
# FREE EXPORT ALLOWANCE
# ===========================================================================
# One free PDF/CSV export per account per scope, then Pro. This used to live
# in the app's SharedPreferences, which made it advisory: clearing app data
# handed the user a fresh allowance, indefinitely. It is an entitlement, so it
# is counted where entitlements are counted.
#
# The client still holds a local copy for the offline case — export works with
# no signal, and hard-failing an offline export to protect a soft gate would
# be a bad trade. When the app is online, the server is authoritative.
# ===========================================================================

_EXPORT_SCOPES = ("estimate", "expense")


def _export_claim_key(user, scope):
    return _idem("export", user, scope)


def _export_claimed(user, scope):
    return bool(
        frappe.db.exists("Export Claim", {"claim_key": _export_claim_key(user, scope)})
    )


@frappe.whitelist()
def export_quota_status():
    """What the caller may export right now, per scope.

    Read-only. The app calls this to decide whether to warn "this uses your one
    free export" before the user spends it.
    """
    try:
        _require_auth()
    except frappe.AuthenticationError:
        return _error_response("UNAUTHORIZED", "Authentication required", 401)

    user = frappe.session.user
    try:
        is_premium = ai_cfg.is_premium(user)
        scopes = {}
        for scope in _EXPORT_SCOPES:
            used = False if is_premium else _export_claimed(user, scope)
            scopes[scope] = {
                "free_used": used,
                "allowed": True if is_premium else not used,
            }
        return {"premium": is_premium, "scopes": scopes}
    except Exception:
        corr = _log_unhandled("v1.export_quota_status")
        return _error_response(
            "SERVER_ERROR", "Failed to load export quota", 500, correlation_id=corr
        )


@frappe.whitelist(methods=["POST"])
def claim_export(scope=None, **kwargs):
    """Spend the free export for `scope`, or confirm Pro entitlement.

    Returns {"allowed": bool, "consumed_free": bool, "premium": bool}.
    402 when the free export for that scope is already gone.

    Call this BEFORE generating the document. It is the gate, not a log: a
    caller that skips it and exports anyway is not counted, which is why the
    client must treat a 402 as final.
    """
    try:
        _require_auth()
    except frappe.AuthenticationError:
        return _error_response("UNAUTHORIZED", "Authentication required", 401)

    body = _read_json_body() or {}
    scope = (scope or body.get("scope") or "").strip().lower()
    if scope not in _EXPORT_SCOPES:
        return _error_response(
            "INVALID_PARAMS",
            "scope must be one of: %s" % ", ".join(_EXPORT_SCOPES),
            400,
        )

    user = frappe.session.user
    try:
        if ai_cfg.is_premium(user):
            # Unlimited. Deliberately records nothing: a lapsed subscriber
            # should fall back to an unspent free export, not to one that Pro
            # quietly burned on their behalf.
            return {"allowed": True, "consumed_free": False, "premium": True}

        doc = frappe.new_doc("Export Claim")
        doc.user = user
        doc.scope = scope
        doc.claim_key = _export_claim_key(user, scope)
        doc.claimed_at = frappe.utils.now_datetime()
        try:
            doc.insert(ignore_permissions=True)
        except (frappe.UniqueValidationError, frappe.DuplicateEntryError):
            # Already spent — either earlier, or by a request that raced this
            # one. Both mean the same thing to the caller.
            frappe.db.rollback()
            return _error_response(
                "EXPORT_QUOTA_EXCEEDED",
                "You have used your free export. Subscribe for unlimited PDF "
                "and CSV reports.",
                402,
                details={"scope": scope},
            )
        frappe.db.commit()
        return {"allowed": True, "consumed_free": True, "premium": False}
    except Exception:
        corr = _log_unhandled("v1.claim_export")
        return _error_response(
            "SERVER_ERROR", "Failed to claim the export", 500, correlation_id=corr
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


# ============================================================================
# G1/G2 reward credits — REMOVED
# ============================================================================
#
# `ad_reward` and `share_reward` granted +1 credit each, capped at one per
# user per day. Neither could verify the thing it paid for: nothing proved a
# rewarded ad had been watched (that needs AdMob server-side verification,
# which was never wired), and a "share" is unverifiable by construction. Any
# client holding a valid token could POST for a free credit a day.
#
# Both routes are gone, along with `_claim_daily_reward` and the
# `rewarded_ads` / `share_rewards` flags. If rewarded ads come back, they
# need a real AdMob unit AND an SSV callback that grants from Google's
# signed payload rather than from a client request — the credit must be
# minted by the callback, never by a route the app can call directly.
# ============================================================================

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

    data = _read_json_body()
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
            idempotency_key=(
                _idem("devgrant", target, data.get("idempotency_key"))
                if data.get("idempotency_key")
                else None
            ),
            period_month=data.get("period_month")
                or (_current_period_month(target) if bucket == "monthly" else None),
        )
        frappe.db.commit()
        return {"ok": True, "balance_after": balance_after}
    except Exception:
        corr = _log_unhandled("v1.dev_grant_credits")
        return _error_response("SERVER_ERROR", "Failed to grant credits", 500, correlation_id=corr)

# ===========================================================================
# CREDIT PRICING (PREM-4)
# ===========================================================================
# What a submit costs against the credit ledger:
#
#   cost = variations * (hd ? 2 : 1)
#
# This is unconditional. It used to sit behind a `credit_gating` feature flag
# so the older free-daily-quota model could stay in force until IAP shipped;
# IAP has shipped, and the flag had become the one row whose absence made
# every paid generation free — it was never seeded, and a missing row read as
# "off". The flag is gone.
#
# Floor-plan analysis is priced at zero: it costs near-nothing on the vendor
# side and is the hook product. A debit of 0 bounds nothing, so that tool is
# held to the per-day free quota instead.
# ===========================================================================

_CREDIT_HD_MULTIPLIER = 2


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
# Idempotent: re-calling with the same `device_id` AND the same
# `device_secret` returns the same row's keys. The row's email is namespaced
# (`anon-<device_id>@buildcost.anon`) so it can never collide with a real user.
#
# `device_secret` is what makes this safe. `device_id` is a client-chosen
# string the app publishes in several places — a URL query parameter to the
# feature-flag endpoint (so it lands in every nginx access log), the body of
# the unauthenticated client-error endpoint, and `obfuscatedAccountId` in
# Google Play. It is an identifier, not a credential, and it was being
# accepted as proof of identity: name any account's device_id and this handed
# you its API secret. The secret is generated on the device, stored only
# there, and only its SHA-256 ever reaches the server (see `Anon Device`).
# ===========================================================================

_ANON_EMAIL_DOMAIN = "buildcost.anon"


def _sha256_hex(value):
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def _attest_device(integrity_token, device_id, binding_doc=None):
    """Evaluate the device's Play Integrity token and record the verdict.

    Returns {"allow_grant": bool, "reason": str}.

    Fails OPEN on every kind of *our* failure — unconfigured, Google
    unreachable, a bug in the decode path. An outage on Google's side must not
    turn into "nobody who installs the app today gets their welcome credits",
    which is a self-inflicted product outage in exchange for no security: an
    attacker cannot cause that outage, so failing closed here buys nothing and
    costs real users. It fails CLOSED only on a verdict that actually came back
    negative, which is the case attestation exists to catch.
    """
    from construction.api import play_integrity

    mode = play_integrity.mode()
    if mode == play_integrity.MODE_OFF:
        return {"allow_grant": True, "reason": "OFF"}

    try:
        result = play_integrity.evaluate(integrity_token, device_id)
    except play_integrity.PlayIntegrityError as e:
        frappe.log_error(str(e), "anon_bootstrap.attestation_unavailable")
        return {"allow_grant": True, "reason": "UNAVAILABLE"}
    except Exception:
        frappe.log_error(frappe.get_traceback(), "anon_bootstrap.attestation_error")
        return {"allow_grant": True, "reason": "ERROR"}

    # Record what we saw, whatever the mode. In `audit` this is the whole
    # point: it is how you find out what the real verdict distribution looks
    # like before you start withholding anything on the strength of it.
    if binding_doc is not None:
        try:
            frappe.db.set_value(
                "Anon Device",
                binding_doc.name,
                {
                    "attestation_verdict": play_integrity.summarize(result),
                    "attested_at": frappe.utils.now_datetime(),
                    "attestation_mode": mode,
                },
                update_modified=False,
            )
        except Exception:
            frappe.log_error(
                frappe.get_traceback(), "anon_bootstrap.attestation_record"
            )

    if mode == play_integrity.MODE_AUDIT:
        return {"allow_grant": True, "reason": "AUDIT_%s" % result["reason"]}

    # enforce
    return {"allow_grant": bool(result["passed"]), "reason": result["reason"]}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def anon_bootstrap(**kwargs):
    """Create / return the anonymous account owned by this device.

    Body: {"device_id": "<uuid>",
           "device_secret": "<32+ random chars>",
           "integrity_token": "<Play Integrity token>"}   # optional
    Returns: {"anon_user_id", "api_key", "api_secret", "created",
              "welcome_granted", "attestation"}

    The device generates `device_secret` once, keeps it, and presents it on
    every bootstrap. Only its hash is stored. A caller who knows a
    `device_id` but not its secret gets a 403 and learns nothing — the
    correct response is to generate a fresh device identity, which yields a
    fresh empty account rather than someone else's.

    One exception, for accounts created before `Anon Device` existed: they
    have no secret on file, so the first secret presented adopts them and
    binds them from then on. See the branch below for why that beats
    stranding those users.

    `integrity_token` is Google's signed statement that this is a genuine,
    Play-installed copy of the app on a genuine device. It gates the WELCOME
    CREDIT GRANT, not account creation — see `api/play_integrity.py` for why,
    and for the off/audit/enforce rollout ladder. Account creation stays open
    to every caller, including old clients that send no token at all.
    """
    data = _read_json_body()
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

    device_secret = (data.get("device_secret") or "").strip()
    if len(device_secret) < 32 or len(device_secret) > 256:
        return _error_response(
            "INVALID_PARAMS",
            "device_secret must be a 32-256 char client-generated random string",
            400,
        )

    # Optional. Absent on old clients and on devices that cannot attest; both
    # still get an account.
    integrity_token = (data.get("integrity_token") or "").strip()

    device_hash = _sha256_hex(device_id)
    secret_hash = _sha256_hex(device_secret)
    email = f"anon-{device_id}@{_ANON_EMAIL_DOMAIN}"
    client_ip = _client_ip()

    try:
        binding = frappe.db.get_value(
            "Anon Device",
            device_hash,
            ["name", "user", "secret_hash", "bootstrap_count"],
            as_dict=True,
        )
        if binding:
            # Constant-time compare so the endpoint can't be used as an oracle.
            import hmac

            if not hmac.compare_digest(binding.secret_hash or "", secret_hash):
                frappe.log_error(
                    f"anon_bootstrap: secret mismatch for device {device_hash[:16]}"
                    f" from {client_ip}",
                    "v1.anon_bootstrap.denied",
                )
                return _error_response(
                    "DEVICE_CLAIMED",
                    "This device identity is already registered. Generate a new one.",
                    403,
                )
            api_key, api_secret = _user_keys(binding.user)
            frappe.db.set_value(
                "Anon Device",
                binding.name,
                {
                    "last_seen_at": frappe.utils.now_datetime(),
                    "bootstrap_count": int(binding.bootstrap_count or 0) + 1,
                },
                update_modified=False,
            )
            frappe.db.commit()
            return {
                "anon_user_id": binding.user,
                "api_key": api_key,
                "api_secret": api_secret,
                "created": False,
            }

        # No binding row. A pre-existing User for this email is an account
        # created before `Anon Device` existed, so there is no secret on file
        # to check against — the row simply predates the mechanism.
        #
        # Refusing it outright stranded those users permanently: the client
        # rotates to a new device identity on 403, which silently abandons
        # their history and credits, and "Restore purchases" then fails
        # forever because `_reject_foreign_receipt` sees the receipt bound to
        # the account they just walked away from. That is a worse outcome
        # than the hole being closed.
        #
        # So a LEGACY account is adopted by the first secret presented for it,
        # and bound from then on. The exposure is one bootstrap per legacy
        # account, closing permanently the moment a real device claims it —
        # strictly narrower than the old behaviour, where any caller naming
        # the device id could take the account at any time, repeatedly. New
        # accounts never take this path; they are bound at creation below.
        legacy_user = frappe.db.get_value("User", {"email": email}, "name")
        if legacy_user:
            # Adoption is the one path that hands over an EXISTING account, so
            # it gets its own tight per-IP budget. A real device adopts once,
            # ever; anyone working through device ids harvested from old nginx
            # logs runs out almost immediately.
            if not _ip_daily_allowed(
                "anon_adopt_ip", _ADOPT_IP_DAILY_CAP, ip=client_ip
            ):
                frappe.log_error(
                    f"anon_bootstrap: adoption cap hit from {client_ip}",
                    "v1.anon_bootstrap.adopt_capped",
                )
                return _error_response(
                    "RATE_LIMITED",
                    "Too many device registrations from this network today. "
                    "Try again later.",
                    429,
                )
            adopted = frappe.new_doc("Anon Device")
            adopted.device_hash = device_hash
            adopted.user = legacy_user
            adopted.secret_hash = secret_hash
            adopted.created_ip = (client_ip or "")[:45]
            adopted.last_seen_at = frappe.utils.now_datetime()
            adopted.bootstrap_count = 1
            try:
                adopted.insert(ignore_permissions=True)
            except (frappe.UniqueValidationError, frappe.DuplicateEntryError):
                # Another request bound it first. Whoever won owns it; this
                # caller has to re-present against the stored secret.
                frappe.db.rollback()
                return _error_response(
                    "DEVICE_CLAIMED",
                    "This device identity is already registered. "
                    "Generate a new one.",
                    403,
                )
            frappe.log_error(
                f"anon_bootstrap: adopted pre-binding account {legacy_user} "
                f"for device {device_hash[:16]} from {client_ip}",
                "v1.anon_bootstrap.legacy_adopted",
            )
            api_key, api_secret = _user_keys(legacy_user)
            frappe.db.commit()
            # No welcome grant: this account already had one, and
            # `_grant_welcome_credits` would dedupe it anyway.
            return {
                "anon_user_id": legacy_user,
                "api_key": api_key,
                "api_secret": api_secret,
                "created": False,
            }

        # F-30: creation is unauthenticated by design, so the only thing
        # bounding it is this per-IP daily budget.
        if not _ip_daily_allowed("anon_bootstrap_ip", _BOOTSTRAP_IP_DAILY_CAP, ip=client_ip):
            return _error_response(
                "RATE_LIMITED",
                "Too many new devices from this network today. Try again later.",
                429,
            )

        user_name = None
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

        # Bind the account to this device BEFORE issuing keys, so a crash
        # between the two can never leave an unclaimable account behind that
        # the next caller could take over.
        binding_doc = frappe.new_doc("Anon Device")
        binding_doc.device_hash = device_hash
        binding_doc.user = user_name
        binding_doc.secret_hash = secret_hash
        binding_doc.created_ip = (client_ip or "")[:45]
        binding_doc.last_seen_at = frappe.utils.now_datetime()
        binding_doc.bootstrap_count = 1
        binding_doc.insert(ignore_permissions=True)

        # _user_keys returns a (api_key, api_secret) tuple.
        api_key, api_secret = _user_keys(user_name)

        # Grant the one-time welcome design credits, anchored on the device so a
        # given device can only ever claim them once (idempotent). Only on first
        # creation of this device row, and only within the per-IP daily cap, to
        # slow credit farming via rotating device_ids. A failed/denied grant
        # never blocks the auth handshake.
        attestation = _attest_device(integrity_token, device_id, binding_doc)

        welcome_granted = False
        if created and attestation["allow_grant"] and _welcome_ip_allowed(ip=client_ip):
            try:
                _grant_welcome_credits(user_name, device_id=device_id)
                welcome_granted = True
            except Exception:
                frappe.log_error(frappe.get_traceback(), "anon_bootstrap.welcome_grant")

        frappe.db.commit()
        return {
            "anon_user_id": user_name,
            "api_key": api_key,
            "api_secret": api_secret,
            "created": created,
            "welcome_granted": welcome_granted,
            "attestation": attestation["reason"],
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
# AUTH-2 server tail: `receipt_token` is validated against the Play
# Developer API (see play_billing.py) before any grant. Fail-closed when the
# service account isn't configured — there is no QA bypass.
# ===========================================================================

# Authoritative price list — what each Play Console product is worth in
# credits. Mirror this when you flip `iap_enabled` on in production.
_TOPUP_GRANT_TABLE = {
    "credits_pack_starter_v1": 25,
    "credits_pack_plus_v1": 80,
    "credits_pack_pro_v1": 250,
}


def _topup_dedup_key(product_id, receipt_token, purchase_id):
    """Stable per-purchase key so re-submits don't double-grant.

    Deliberately GLOBAL, not per-user: the same receipt must not be grantable
    twice no matter who presents it. `_claim_topup_receipt` below turns that
    into an ownership check, because a global key on its own silently returned
    "already granted" to a second user replaying someone else's receipt —
    a false success where the honest answer is "that isn't yours".
    """
    src = f"{product_id}|{purchase_id or ''}|{receipt_token or ''}"
    return "topup:" + hashlib.sha256(src.encode("utf-8")).hexdigest()[:48]


def _claim_topup_receipt(user, dedup_key):
    """Who, if anyone, has already redeemed this receipt.

    Returns (owner_user or None). Callers reject a mismatch rather than
    reporting a grant that never happened.
    """
    return frappe.db.get_value(
        "AI Credit Ledger", {"idempotency_key": dedup_key}, "user"
    )


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

    # Body only — see `_read_json_body`. A receipt is not a query parameter.
    data = _read_json_body()
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

    # AUTH-2: verify the token against the Play Developer API before granting.
    # Fail-closed when validation isn't configured — an unverified string must
    # never mint credits, and there is no bypass to opt into.
    from construction.api import play_billing

    play_billing.warn_if_relaxed_configured()

    try:
        verified = play_billing.verify_product(receipt_token, product_id)
    except play_billing.PlayBillingError as e:
        return _error_response(e.code, e.message, e.http_status)
    if verified["purchase_state"] == play_billing.PRODUCT_STATE_PENDING:
        return _error_response(
            "RECEIPT_PENDING",
            "This purchase is still pending. Credits are granted once it completes.",
            409,
        )
    if verified["purchase_state"] != play_billing.PRODUCT_STATE_PURCHASED:
        return _error_response(
            "RECEIPT_NOT_PURCHASED",
            "This purchase is not in a purchased state according to Google Play.",
            400,
        )
    # Prefer Play's order id for dedup — it's server-issued and stable across
    # client retries even if the client mangles its own ids.
    if verified.get("order_id"):
        purchase_id = verified["order_id"]

    grant_amount = _TOPUP_GRANT_TABLE[product_id]
    dedup_key = _topup_dedup_key(product_id, receipt_token, purchase_id)
    user = frappe.session.user

    owner = _claim_topup_receipt(user, dedup_key)
    if owner and owner != user:
        frappe.log_error(
            f"grant_topup_credits: receipt owned by {owner}, replayed by {user}",
            "v1.grant_topup_credits.foreign_receipt",
        )
        return _error_response(
            "RECEIPT_FOREIGN",
            "This purchase belongs to a different account.",
            403,
        )

    try:
        with _user_ledger_lock(user):
            balance_after = _grant_topup_credits(
                user,
                amount=grant_amount,
                idempotency_key=dedup_key,
                reason=f"Top-up: {product_id}",
                ref_name=purchase_id or product_id,
            )
        frappe.db.commit()
        state = _get_credit_state(user)
        return {
            "ok": True,
            "granted": grant_amount,
            "balance_after": balance_after,
            "balance": state,
        }
    except LedgerLockError:
        return _error_response("BUSY", "Please try again in a moment.", 409)
    except Exception:
        corr = _log_unhandled("v1.grant_topup_credits")
        return _error_response(
            "SERVER_ERROR",
            "Failed to grant top-up credits",
            500,
            correlation_id=corr,
        )

