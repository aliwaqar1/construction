import frappe

@frappe.whitelist(allow_guest=True)
def get_estimate(city, material_quality, sqft: float):
    """
    Returns detailed material-wise construction estimate
    Args:
        city (str): e.g. 'Mumbai'
        material_quality (str): e.g. 'Medium'
        sqft (float): area in square feet
    """
    sqft = float(sqft)
    estimate_data = []

    # Find the Construction Material document
    cm_doc = frappe.get_all(
        "Construction Material",
        filters={"city": city, "material_quality": material_quality},
        fields=["name"]
    )

    if not cm_doc:
        return {"error": f"No data found for {city} ({material_quality})"}

    cm_name = cm_doc[0]["name"]

    # Get all child materials
    materials = frappe.get_all(
        "Material Rate",
        filters={"parent": cm_name},
        fields=["material", "uom", "rate"]
    )

    for m in materials:
        # Get ratio from Material Master
        ratio = frappe.db.get_value("Material Master", m["material"], "ratio_per_sqft") or 0
        estimated_qty = ratio * sqft
        estimated_cost = estimated_qty * m["rate"]

        estimate_data.append({
            "material": m["material"],
            "uom": m["uom"],
            "rate": m["rate"],
            "ratio_per_sqft": ratio,
            "total_qty": round(estimated_qty, 3),
            "total_cost": round(estimated_cost, 2),
        })

    total_sum = sum(i["total_cost"] for i in estimate_data)

    return {
        "city": city,
        "material_quality": material_quality,
        "sqft": sqft,
        "materials": estimate_data,
        "grand_total": round(total_sum, 2)
    }

@frappe.whitelist(allow_guest=True)
def get_city_quality():
    """
    Return list of cities and their available qualities from Construction Material
    Example output:
    {
        "cities": ["Mumbai", "Delhi"],
        "qualities": {
            "Mumbai": ["Premium", "Medium", "Low"],
            "Delhi": ["Medium", "Economy"]
        }
    }
    """
    data = frappe.db.sql("""
        SELECT DISTINCT city, material_quality
        FROM `tabConstruction Material`
        WHERE city IS NOT NULL AND material_quality IS NOT NULL
        ORDER BY city, material_quality
    """, as_dict=True)

    cities = []
    qualities = {}

    for d in data:
        city = d.city.strip()
        quality = d.material_quality.strip()

        if city not in cities:
            cities.append(city)
            qualities[city] = []
        if quality not in qualities[city]:
            qualities[city].append(quality)

    return {
        "cities": cities,
        "qualities": qualities
    }
