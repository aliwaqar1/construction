"""
V1 API endpoints for the questionnaire-driven estimate engine.

Frappe URLs (all allow_guest):
    GET  /api/method/construction.api.v1.countries
    GET  /api/method/construction.api.v1.cities?country=PK
    GET  /api/method/construction.api.v1.questionnaire?country=PK&city=Lahore&flow=house_estimate_v1
    POST /api/method/construction.api.v1.estimate   {country, city, plot_size_sqft, covered_area_sqft, answers:{…}}
"""

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
