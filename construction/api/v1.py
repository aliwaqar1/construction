"""
V1 API endpoints for the questionnaire-driven estimate engine.

Frappe URLs (all allow_guest):
    GET  /api/method/construction.api.v1.countries
    GET  /api/method/construction.api.v1.cities?country=PK
    GET  /api/method/construction.api.v1.questionnaire?country=PK&city=Lahore&flow=house_estimate_v1
    POST /api/method/construction.api.v1.estimate   {country, city, plot_size_sqft, covered_area_sqft, answers:{…}}
"""

import json

import frappe

from construction.estimate_engine.engine import resolve_estimate


# ---------------------------------------------------------------------------
# GET /v1/meta/countries
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=True)
def countries():
    """Return all enabled estimate countries."""
    rows = frappe.get_all(
        "Estimate Country",
        filters={"enabled": 1},
        fields=["code", "country_name", "currency"],
        order_by="country_name asc",
    )
    return {"countries": rows}


# ---------------------------------------------------------------------------
# GET /v1/meta/cities?country=PK
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=True)
def cities(country=None):
    """Return enabled cities, optionally filtered by country code."""
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


# ---------------------------------------------------------------------------
# GET /v1/questionnaire?country=PK&city=Lahore&flow=house_estimate_v1
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=True)
def questionnaire(country=None, city=None, flow=None):
    """
    Return the questionnaire definition (ordered steps with questions,
    options, recommended flags, and conditional visibility).
    """
    if not country or not flow:
        frappe.throw("'country' and 'flow' are required query parameters")

    docs = frappe.get_all(
        "Estimate Questionnaire",
        filters={"country": country, "flow_key": flow},
        fields=["flow_key", "country", "version", "title", "description", "steps_json"],
        limit=1,
    )

    if not docs:
        frappe.throw(
            f"No questionnaire found for country={country}, flow={flow}",
            exc=frappe.DoesNotExistError,
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


# ---------------------------------------------------------------------------
# POST /v1/estimate
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=True, methods=["POST"])
def estimate(**kwargs):
    """
    Compute a construction estimate.

    Expects JSON body::

        {
            "country": "PK",
            "city": "Lahore",
            "plot_size_sqft": 1125,
            "covered_area_sqft": 1125,
            "answers": {
                "structure_type": "gray",
                "drawing_required": "no",
                "brick_type": "awal",
                ...
            }
        }

    Returns::

        {
            "line_items": [{material, material_key, phase, qty, unit, rate, cost}, ...],
            "totals": {"gray": ..., "finish": ..., "overall": ...},
            "phase_breakdown": { ... }   // optional, present when finish is included
        }
    """
    # Accept both form-encoded params and JSON body
    data = kwargs
    if frappe.request and frappe.request.is_json:
        data = frappe.parse_json(frappe.request.data)

    country = data.get("country")
    city = data.get("city")
    plot_size = data.get("plot_size_sqft")
    covered_area = data.get("covered_area_sqft")
    answers = data.get("answers") or {}

    # --- validation ---
    if not country:
        frappe.throw("'country' is required")
    if not city:
        frappe.throw("'city' is required")
    if plot_size is None:
        frappe.throw("'plot_size_sqft' is required")
    if covered_area is None:
        frappe.throw("'covered_area_sqft' is required")

    try:
        plot_size = float(plot_size)
        covered_area = float(covered_area)
    except (TypeError, ValueError):
        frappe.throw("'plot_size_sqft' and 'covered_area_sqft' must be numeric")

    if plot_size <= 0 or covered_area <= 0:
        frappe.throw("'plot_size_sqft' and 'covered_area_sqft' must be positive")

    if not isinstance(answers, dict):
        frappe.throw("'answers' must be a JSON object / dict")

    # --- compute ---
    result = resolve_estimate(country, city, plot_size, covered_area, answers)
    return result
