"""
Generic questionnaire-driven estimate engine.

Workflow:
1. Load questionnaire definition (steps_json) for country + flow_key
2. Walk through user answers, collect impact dicts from matching options
3. Merge impacts deterministically (step order → question order)
4. Route to country-specific calculator with merged params
"""

import json

import frappe

from construction.estimate_engine.in_calculator import compute as compute_in
from construction.estimate_engine.pk_calculator import compute as compute_pk

# ---------------------------------------------------------------------------
# Country calculator registry
# ---------------------------------------------------------------------------
_CALCULATORS = {
    "PK": compute_pk,
    "IN": compute_in,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def resolve_estimate(country, city, plot_size_sqft, covered_area_sqft, answers):
    """
    Main entry point.  Returns a standardised estimate dict.

    Parameters
    ----------
    country : str          – ISO-2 code, e.g. "PK", "IN"
    city : str             – city name, e.g. "Lahore"
    plot_size_sqft : float
    covered_area_sqft : float – total covered area; sum of per-floor areas
                                when provided.
    answers : dict         – flat map of question_key → selected option value.
                             May contain `floor_areas` (list[number]) and
                             `num_floors` (int) for per-floor estimating.
    """
    plot_size_sqft = float(plot_size_sqft)
    covered_area_sqft = float(covered_area_sqft)

    floor_areas = _extract_floor_areas(answers, covered_area_sqft)
    user_rate_overrides = _extract_rate_overrides(answers)

    # 1. Load questionnaire
    q_doc = frappe.get_all(
        "Estimate Questionnaire",
        filters={"country": country, "flow_key": "house_estimate_v1"},
        fields=["steps_json"],
        limit=1,
    )
    steps_data = json.loads(q_doc[0].steps_json) if q_doc else {"steps": []}

    # 2. Merge impacts
    impacts = merge_impacts(steps_data, answers)
    params = impacts["params"]

    # 3. Route to calculator
    calculator = _CALCULATORS.get(country)
    if not calculator:
        frappe.throw(f"No calculator registered for country '{country}'")

    return calculator(
        city,
        plot_size_sqft,
        covered_area_sqft,
        params,
        floor_areas=floor_areas,
        user_rate_overrides=user_rate_overrides,
    )


def _extract_floor_areas(answers, covered_area_sqft):
    """Pull `floor_areas` out of answers, falling back to a single floor.

    Returns a list of positive floats. Always returns at least one entry so
    calculators can index `[0]` safely.
    """
    raw = (answers or {}).get("floor_areas")
    if isinstance(raw, list) and raw:
        floors = []
        for v in raw:
            try:
                f = float(v)
            except (TypeError, ValueError):
                continue
            if f > 0:
                floors.append(f)
        if floors:
            return floors
    return [covered_area_sqft]


def _extract_rate_overrides(answers):
    """Pull Pro per-material rate overrides out of answers.

    Shape: {material_key: number}. Recognised keys: gray_per_sqft,
    finish_per_sqft, cement_bag, saria_kg, brick. Unknown keys
    are forwarded to calculators verbatim so future overrides do not require
    an engine update.
    """
    raw = (answers or {}).get("rate_overrides")
    if not isinstance(raw, dict):
        return {}
    out = {}
    for k, v in raw.items():
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f > 0:
            out[str(k)] = f
    return out



# ---------------------------------------------------------------------------
# Impact merger
# ---------------------------------------------------------------------------

def merge_impacts(questionnaire_data, answers):
    """
    Walk steps → questions → options.  For each answer that matches an
    option value, collect its ``impact`` dict and merge into the accumulator.

    Merge order is deterministic: steps sorted by ``order``, questions by
    their position in the list.

    Returns
    -------
    dict with keys:
        params          – flat dict of calculator parameters
        rate_overrides  – {material_key: value_or_settings_field}
        qty_factors     – {material_key: value_or_settings_field}
        enabled_items   – {material_key: bool}
        enabled_phases  – set of phase names (gray is always present)
    """
    merged = {
        "params": {},
        "rate_overrides": {},
        "qty_factors": {},
        "enabled_items": {},
        "enabled_phases": {"gray"},
    }

    steps = questionnaire_data.get("steps", [])
    for step in sorted(steps, key=lambda s: s.get("order", 0)):
        for question in step.get("questions", []):
            q_key = question["key"]
            answer_value = answers.get(q_key)
            if answer_value is None:
                continue

            # Normalise answer to string for comparison
            answer_str = str(answer_value)

            for option in question.get("options", []):
                if str(option["value"]) == answer_str:
                    impact = option.get("impact") or {}
                    merged["params"].update(impact.get("set_param", {}))
                    merged["rate_overrides"].update(impact.get("rate_override", {}))
                    merged["qty_factors"].update(impact.get("qty_factor", {}))
                    merged["enabled_items"].update(impact.get("enable_item", {}))
                    if "enable_phase" in impact:
                        merged["enabled_phases"].add(impact["enable_phase"])
                    break

    return merged
