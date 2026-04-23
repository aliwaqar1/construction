# Sample Request / Response — V1 Estimate API

All URLs use the Frappe convention `GET|POST /api/method/<dotted.path>`.

---

## 1. GET Countries

```
GET /api/method/construction.api.v1.countries
```

### Response
```json
{
  "message": {
    "countries": [
      {"code": "PK", "country_name": "Pakistan", "currency": "PKR"},
      {"code": "IN", "country_name": "India",    "currency": "INR"}
    ]
  }
}
```

---

## 2. GET Cities

```
GET /api/method/construction.api.v1.cities?country=PK
```

### Response
```json
{
  "message": {
    "cities": [
      {"city_name": "Faisalabad", "country_code": "PK"},
      {"city_name": "Islamabad",  "country_code": "PK"},
      {"city_name": "Karachi",    "country_code": "PK"},
      {"city_name": "Lahore",     "country_code": "PK"},
      {"city_name": "Multan",     "country_code": "PK"},
      {"city_name": "Rawalpindi", "country_code": "PK"}
    ]
  }
}
```

---

## 3. GET Questionnaire (Pakistan)

```
GET /api/method/construction.api.v1.questionnaire?country=PK&flow=house_estimate_v1
```

### Response
```json
{
  "message": {
    "flow_key": "house_estimate_v1",
    "country": "PK",
    "version": 1,
    "title": "House Construction Estimate",
    "description": "Pakistan house construction estimate — gray structure and finishing options.",
    "steps": [
      {
        "step_key": "house_info",
        "title": "House Information",
        "order": 1,
        "visibility": null,
        "questions": [
          {
            "key": "structure_type",
            "label": "Structure Type",
            "input_type": "radio",
            "required": true,
            "options": [
              {"value": "gray",     "label": "Gray Structure Only", "is_recommended": true, "impact": {"set_param": {"construction_type": "gray"}}},
              {"value": "finished", "label": "Gray + Finished",     "impact": {"set_param": {"construction_type": "both"}, "enable_phase": "finish"}}
            ]
          },
          {
            "key": "drawing_required",
            "label": "Drawing Required?",
            "input_type": "radio",
            "required": true,
            "options": [
              {"value": "yes", "label": "Yes", "impact": {"set_param": {"drawingRequired": true}}},
              {"value": "no",  "label": "No",  "is_recommended": true, "impact": {"set_param": {"drawingRequired": false}}}
            ]
          }
        ]
      },
      {
        "step_key": "gray_structure",
        "title": "Gray Structure Options",
        "order": 2,
        "visibility": null,
        "questions": [
          {"key": "brick_type",      "label": "Brick Type (Ent)",    "input_type": "radio", "required": true, "options": ["...see fixture..."]},
          {"key": "foundation_depth","label": "Foundation Depth",     "input_type": "radio", "required": true, "options": ["..."]},
          {"key": "termite_spray",   "label": "Termite Spray",       "input_type": "radio", "required": true, "options": ["..."]},
          {"key": "steel_type",      "label": "Steel Type (Saria)",  "input_type": "radio", "required": true, "options": ["..."]},
          {"key": "bore",            "label": "Pump Bore Required?", "input_type": "radio", "required": true, "options": ["..."]},
          {"key": "electric_pipe",   "label": "Electric Pipe Type",  "input_type": "radio", "required": true, "options": ["..."]},
          {"key": "sanitary_pipe",   "label": "Sanitary Pipe Type",  "input_type": "radio", "required": true, "options": ["..."]},
          {"key": "gate_gauge",      "label": "Gate Gauge",          "input_type": "radio", "required": true, "options": ["..."]}
        ]
      },
      {
        "step_key": "finish",
        "title": "Finishing Options",
        "order": 3,
        "visibility": {"when": "structure_type", "eq": "finished"},
        "questions": [
          {"key": "floor_type",       "label": "Floor Type",                    "input_type": "radio", "required": true, "options": ["..."]},
          {"key": "wiring",           "label": "Electrical Wiring",             "input_type": "radio", "required": true, "options": ["..."]},
          {"key": "window_type",      "label": "Window Type",                   "input_type": "radio", "required": true, "options": ["..."]},
          {"key": "paint_filling",    "label": "Paint with Filling?",           "input_type": "radio", "required": true, "options": ["..."]},
          {"key": "wood_sheet",       "label": "Wood Sheet Type",               "input_type": "radio", "required": true, "options": ["..."]},
          {"key": "washroom_fitting", "label": "Washroom / Sanitary Fitting",   "input_type": "radio", "required": true, "options": ["..."]},
          {"key": "ceiling",          "label": "Ceiling Required?",             "input_type": "radio", "required": true, "options": ["..."]}
        ]
      }
    ]
  }
}
```

---

## 4. POST Estimate — Pakistan (Gray Only)

```
POST /api/method/construction.api.v1.estimate
Content-Type: application/json

{
  "country": "PK",
  "city": "Lahore",
  "plot_size_sqft": 1125,
  "covered_area_sqft": 1125,
  "answers": {
    "structure_type": "gray",
    "drawing_required": "no",
    "brick_type": "awal",
    "foundation_depth": "3ft",
    "termite_spray": "yes",
    "steel_type": "branded",
    "bore": "no",
    "electric_pipe": "branded",
    "sanitary_pipe": "branded",
    "gate_gauge": "16"
  }
}
```

### Response (values depend on Construction Setting rates)
```json
{
  "message": {
    "line_items": [
      {"material": "Foundation",            "material_key": "foundation",      "phase": "gray", "qty": 1687,   "unit": "SF",   "rate": 100.0,  "cost": 168750.0},
      {"material": "Termite Spray",         "material_key": "termite",         "phase": "gray", "qty": 1125,   "unit": "SF",   "rate": 5.0,    "cost": 5625.0},
      {"material": "Rori",                  "material_key": "rori",            "phase": "gray", "qty": 562,    "unit": "SF",   "rate": 10.0,   "cost": 5625.0},
      {"material": "Rait Ravi",             "material_key": "rait_ravi",       "phase": "gray", "qty": 337,    "unit": "SF",   "rate": 8.0,    "cost": 2700.0},
      {"material": "Rait Chanab",           "material_key": "rait_chanab",     "phase": "gray", "qty": 225,    "unit": "SF",   "rate": 7.0,    "cost": 1575.0},
      {"material": "Ent (Bricks)",          "material_key": "ent",             "phase": "gray", "qty": 3375,   "unit": "Pcs",  "rate": 15.0,   "cost": 50625.0},
      {"material": "Kaasoo Bahari",         "material_key": "kaasoo",          "phase": "gray", "qty": 1,      "unit": "Lot",  "rate": 80000,  "cost": 80000.0},
      {"material": "Bajri",                 "material_key": "bajri",           "phase": "gray", "qty": 450,    "unit": "SF",   "rate": 12.0,   "cost": 5400.0},
      {"material": "Cement",                "material_key": "cement",          "phase": "gray", "qty": 675,    "unit": "Bags", "rate": 50.0,   "cost": 33750.0},
      {"material": "Saria (Steel)",         "material_key": "saria",           "phase": "gray", "qty": 1125,   "unit": "KGs",  "rate": 280.0,  "cost": 315000.0},
      {"material": "Bijli Pipes",           "material_key": "bijli",           "phase": "gray", "qty": 1125,   "unit": "SF",   "rate": 50.0,   "cost": 56250.0},
      {"material": "Sanitary Pipes",        "material_key": "sanitary_pipes",  "phase": "gray", "qty": 1125,   "unit": "SF",   "rate": 40.0,   "cost": 45000.0},
      {"material": "Gate & Steel Chaukhat", "material_key": "gate_chaukhat",   "phase": "gray", "qty": 1125,   "unit": "SF",   "rate": 60.0,   "cost": 67500.0},
      {"material": "Labour",                "material_key": "labour",          "phase": "gray", "qty": 1125,   "unit": "SF",   "rate": 350.0,  "cost": 393750.0},
      {"material": "Other Expenses",        "material_key": "other_expenses",  "phase": "gray", "qty": 1,      "unit": "Lot",  "rate": 120000, "cost": 120000.0}
    ],
    "totals": {
      "gray": 1351550.0,
      "overall": 1351550.0
    }
  }
}
```

> **Note:** The exact numbers above use mock settings for illustration. Real values
> come from the `Construction Setting` singleton in your database.

---

## 5. POST Estimate — Pakistan (Gray + Finish)

```
POST /api/method/construction.api.v1.estimate
Content-Type: application/json

{
  "country": "PK",
  "city": "Lahore",
  "plot_size_sqft": 1125,
  "covered_area_sqft": 1125,
  "answers": {
    "structure_type": "finished",
    "drawing_required": "yes",
    "brick_type": "awal_plus",
    "foundation_depth": "4ft",
    "termite_spray": "yes",
    "steel_type": "branded",
    "bore": "yes",
    "electric_pipe": "branded",
    "sanitary_pipe": "branded",
    "gate_gauge": "16",
    "floor_type": "marble",
    "wiring": "branded",
    "window_type": "aluminium",
    "paint_filling": "yes",
    "wood_sheet": "branded",
    "washroom_fitting": "branded",
    "ceiling": "yes"
  }
}
```

### Response (abbreviated)
```json
{
  "message": {
    "line_items": [
      {"material": "Foundation", "material_key": "foundation", "phase": "gray", "qty": 2250, "unit": "SF", "rate": "...", "cost": "..."},
      {"material": "Drawing",   "material_key": "drawing",    "phase": "gray", "qty": 1,    "unit": "Lot","rate": "...", "cost": "..."},
      "... (remaining gray items) ...",
      {"material": "Floor (Marble)",      "material_key": "floor",        "phase": "finish", "qty": 1125, "unit": "m",  "rate": "...", "cost": "..."},
      {"material": "Flooring Labour",     "material_key": "floor_labour", "phase": "finish", "qty": 1125, "unit": "SF", "rate": "...", "cost": "..."},
      "... (remaining finish items) ..."
    ],
    "totals": {
      "gray":    "...",
      "finish":  "...",
      "overall": "..."
    },
    "phase_breakdown": {
      "gray":   {"line_items": ["..."], "total": "..."},
      "finish": {"line_items": ["..."], "total": "..."}
    }
  }
}
```

---

## 6. POST Estimate — India V1

```
POST /api/method/construction.api.v1.estimate
Content-Type: application/json

{
  "country": "IN",
  "city": "Mumbai",
  "plot_size_sqft": 1000,
  "covered_area_sqft": 1000,
  "answers": {
    "material_quality": "Medium"
  }
}
```

### Response
```json
{
  "message": {
    "line_items": [
      {"material": "Cement",     "material_key": "cement",     "phase": "combined", "qty": 120.0, "unit": "Bags", "rate": 400.0, "cost": 48000.0},
      {"material": "Steel TMT",  "material_key": "steel_tmt",  "phase": "combined", "qty": 80.0,  "unit": "KGs",  "rate": 65.0,  "cost": 5200.0},
      {"material": "Sand",       "material_key": "sand",       "phase": "combined", "qty": 50.0,  "unit": "cft",  "rate": 55.0,  "cost": 2750.0},
      {"material": "Aggregate",  "material_key": "aggregate",  "phase": "combined", "qty": 40.0,  "unit": "cft",  "rate": 45.0,  "cost": 1800.0}
    ],
    "totals": {
      "overall": 57750.0
    }
  }
}
```

> **Note:** India line items come from `Construction Material` + `Material Rate` +
> `Material Master` records for that city. If no answers are supplied, defaults
> to `material_quality = "Medium"`. If no data exists for the city, a 417 error
> is returned.

---

## 7. India V1 — Default Assumptions (no answers)

```
POST /api/method/construction.api.v1.estimate
Content-Type: application/json

{
  "country": "IN",
  "city": "Mumbai",
  "plot_size_sqft": 1000,
  "covered_area_sqft": 1000,
  "answers": {}
}
```

The backend defaults `material_quality` to `"Medium"` and returns the same
structure as above.
