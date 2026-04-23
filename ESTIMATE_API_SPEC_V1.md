# Estimate API Spec V1

## Purpose
This document defines the V1 integration contract between the Flutter app and the backend estimate engine for house construction cost estimation.

Audience:
- Flutter developer
- Backend developer

V1 countries:
- Pakistan (`PK`)
- India (`IN`)

The app collects user inputs, sends them to the backend, and receives an itemized estimate with totals.

---

## Scope
V1 supports:
- Country selection
- City selection
- House size inputs
- Pakistan questionnaire flow with gray structure and finish options
- India minimal flow with default assumptions
- Itemized estimate response with totals and optional phase breakdown

V1 does not cover:
- User accounts
- Saved estimates
- PDF exports
- Multi-language content
- Dynamic unit conversion on the client

---

## Base Concepts
The backend estimate engine works in three stages:

1. The app sends house inputs and selected answers.
2. The backend evaluates questionnaire impacts and construction rules.
3. The backend calculates material consumption, fetches city/rule-based rates, and returns line items and totals.

High-level rule behavior:
- Base formulas determine material consumption.
- Selected answers apply impacts.
- Impacts may change multipliers, substitutions, or rate behavior.
- Final rates are resolved by country/city/rule configuration.

---

## Units and Currency
| Item | Value |
|---|---|
| Plot size | `sqft` |
| Covered area | `sqft` |
| Foundation depth options | `3ft`, `4ft` |
| Quantity units in estimate | Depends on material, for example `sqft`, `Bags`, `KGs`, `Pcs`, `Lot`, `m` |
| Pakistan currency | `PKR` |
| India currency | `INR` |

Notes:
- Flutter should send `plot_size_sqft` and `covered_area_sqft` as numeric values.
- Flutter should not convert currencies.
- Flutter should display units exactly as returned by the backend.

---

## Country Flows

### Pakistan (`PK`)
Pakistan uses a questionnaire-driven flow.

Flow code:
- `house_estimate_v1`

Conditional rule for V1:
- The `finish` step is shown only when `structure_type = finished`.

This is the chosen V1 behavior. The Flutter app should not show the finish screen if the user selects `gray`.

### India (`IN`)
India V1 is intentionally minimal.

Required inputs:
- `country`
- `city`
- `plot_size_sqft`
- `covered_area_sqft`

No additional questionnaire answers are required in V1.
The backend uses default assumptions when calculating India estimates.

---

## Pakistan UI Flow

### Step 1: House Info
Screen key:
- `house_info`

Fields:
| Label | Request Key | Type | Allowed Values | Required |
|---|---|---|---|---|
| Plot size (sqft) | `plot_size_sqft` | Number | Any positive number | Yes |
| Covered area (sqft) | `covered_area_sqft` | Number | Any positive number | Yes |
| Structure type | `structure_type` | Radio | `gray`, `finished` | Yes |
| Drawing required | `drawing_required` | Radio | `yes`, `no` | Yes |

### Step 2: Gray Structure Questions
Screen key:
- `gray_structure`

Fields:
| Label | Request Key | Type | Allowed Values | Required |
|---|---|---|---|---|
| Brick type | `brick_type` | Radio | `awal_plus`, `awal`, `dom` | Yes |
| Foundation depth | `foundation_depth` | Radio | `3ft`, `4ft` | Yes |
| Termite spray | `termite_spray` | Radio | `yes`, `no` | Yes |
| Steel type | `steel_type` | Radio | `local_60g`, `branded` | Yes |
| Bore | `bore` | Radio | `yes`, `no` | Yes |
| Electric pipe | `electric_pipe` | Radio | `branded`, `local` | Yes |
| Sanitary pipe | `sanitary_pipe` | Radio | `branded`, `local` | Yes |
| Gate gauge | `gate_gauge` | Radio | `16`, `18` | Yes |

### Step 3: Finish Questions
Screen key:
- `finish`

Visibility rule:
- Show only if `structure_type = finished`

Fields:
| Label | Request Key | Type | Allowed Values | Required |
|---|---|---|---|---|
| Floor type | `floor_type` | Radio | `marble`, `tile` | Yes when finish step is visible |
| Wiring | `wiring` | Radio | `branded`, `local` | Yes when finish step is visible |
| Window type | `window_type` | Radio | `aluminium`, `steel` | Yes when finish step is visible |
| Paint filling | `paint_filling` | Radio | `yes`, `no` | Yes when finish step is visible |
| Wood sheet | `wood_sheet` | Radio | `branded`, `local` | Yes when finish step is visible |
| Washroom fitting | `washroom_fitting` | Radio | `branded`, `local` | Yes when finish step is visible |
| Ceiling | `ceiling` | Radio | `yes`, `no` | Yes when finish step is visible |

---

## Pakistan Question Key Reference
| Step | Key | Allowed Values |
|---|---|---|
| house_info | `structure_type` | `gray`, `finished` |
| house_info | `drawing_required` | `yes`, `no` |
| gray_structure | `brick_type` | `awal_plus`, `awal`, `dom` |
| gray_structure | `foundation_depth` | `3ft`, `4ft` |
| gray_structure | `termite_spray` | `yes`, `no` |
| gray_structure | `steel_type` | `local_60g`, `branded` |
| gray_structure | `bore` | `yes`, `no` |
| gray_structure | `electric_pipe` | `branded`, `local` |
| gray_structure | `sanitary_pipe` | `branded`, `local` |
| gray_structure | `gate_gauge` | `16`, `18` |
| finish | `floor_type` | `marble`, `tile` |
| finish | `wiring` | `branded`, `local` |
| finish | `window_type` | `aluminium`, `steel` |
| finish | `paint_filling` | `yes`, `no` |
| finish | `wood_sheet` | `branded`, `local` |
| finish | `washroom_fitting` | `branded`, `local` |
| finish | `ceiling` | `yes`, `no` |

---

## API Overview
| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/v1/meta/countries` | Fetch supported countries |
| GET | `/v1/meta/cities?country=PK` | Fetch cities for a country |
| GET | `/v1/questionnaire?country=PK&city=Lahore&flow=house_estimate_v1` | Fetch dynamic questionnaire flow |
| POST | `/v1/estimate` | Create estimate |

Implementation note for Frappe deployments:
- If exposed through Frappe method routing, the actual transport URL may be mapped under `/api/method/...`.
- The logical contract for mobile integration remains the V1 paths listed above.

---

## 1. GET `/v1/meta/countries`
Returns supported countries.

### Response Shape
```json
{
  "countries": [
    {
      "code": "PK",
      "name": "Pakistan"
    },
    {
      "code": "IN",
      "name": "India"
    }
  ]
}
```

### Field Definitions
| Field | Type | Description |
|---|---|---|
| `code` | String | Country code, for example `PK`, `IN` |
| `name` | String | Country display name |

---

## 2. GET `/v1/meta/cities?country=PK`
Returns supported cities for a selected country.

### Query Parameters
| Name | Type | Required | Description |
|---|---|---|---|
| `country` | String | Yes | Country code, for example `PK` |

### Response Shape
```json
{
  "cities": [
    {
      "id": "PK-Lahore",
      "name": "Lahore"
    },
    {
      "id": "PK-Karachi",
      "name": "Karachi"
    },
    {
      "id": "PK-Islamabad",
      "name": "Islamabad"
    }
  ]
}
```

### Field Definitions
| Field | Type | Description |
|---|---|---|
| `id` | String | Stable city identifier |
| `name` | String | City display name |

Flutter guidance:
- Store both `id` and `name` if available.
- Use the city `name` or mapped backend identifier consistently when calling questionnaire and estimate endpoints.

---

## 3. GET `/v1/questionnaire?country=PK&city=<city>&flow=house_estimate_v1`
Returns the questionnaire definition for the selected country, city, and flow.

This endpoint exists so Flutter does not hardcode backend business logic.

### Query Parameters
| Name | Type | Required | Description |
|---|---|---|---|
| `country` | String | Yes | Country code |
| `city` | String | Yes | City name or mapped city identifier |
| `flow` | String | Yes | Flow code, V1 uses `house_estimate_v1` |

### Response Requirements
The response must include:
- Flow code
- Ordered steps array
- Questions per step
- Options per question
- Recommended option flags
- Conditional visibility rules

### Response Shape
```json
{
  "flow": "house_estimate_v1",
  "country": "PK",
  "city": "Lahore",
  "steps": [
    {
      "key": "house_info",
      "title": "House Information",
      "order": 1,
      "visibility": null,
      "questions": [
        {
          "key": "structure_type",
          "label": "Structure Type",
          "type": "radio",
          "required": true,
          "options": [
            {
              "value": "gray",
              "label": "Gray Structure Only",
              "recommended": true
            },
            {
              "value": "finished",
              "label": "Gray + Finish",
              "recommended": false
            }
          ]
        },
        {
          "key": "drawing_required",
          "label": "Drawing Required",
          "type": "radio",
          "required": true,
          "options": [
            {
              "value": "yes",
              "label": "Yes",
              "recommended": false
            },
            {
              "value": "no",
              "label": "No",
              "recommended": true
            }
          ]
        }
      ]
    },
    {
      "key": "gray_structure",
      "title": "Gray Structure Questions",
      "order": 2,
      "visibility": null,
      "questions": [
        {
          "key": "brick_type",
          "label": "Brick Type",
          "type": "radio",
          "required": true,
          "options": [
            { "value": "awal_plus", "label": "Awal+", "recommended": false },
            { "value": "awal", "label": "Awal", "recommended": true },
            { "value": "dom", "label": "Dom", "recommended": false }
          ]
        }
      ]
    },
    {
      "key": "finish",
      "title": "Finish Questions",
      "order": 3,
      "visibility": {
        "when": "structure_type",
        "operator": "equals",
        "value": "finished"
      },
      "questions": [
        {
          "key": "floor_type",
          "label": "Floor Type",
          "type": "radio",
          "required": true,
          "options": [
            { "value": "marble", "label": "Marble", "recommended": true },
            { "value": "tile", "label": "Tile", "recommended": false }
          ]
        }
      ]
    }
  ]
}
```

### Visibility Rule Contract
| Field | Meaning |
|---|---|
| `when` | The answer key to evaluate |
| `operator` | Comparison operator, V1 uses `equals` |
| `value` | Value required for visibility |

V1 conditional rule:
- `finish` step is visible only when `structure_type = finished`

Flutter guidance:
- Render steps in ascending `order`.
- Hide a step if its `visibility` condition is not satisfied.
- Use `recommended` only for preselection UX, not as a replacement for explicit user choice.

---

## 4. POST `/v1/estimate`
Creates an estimate using country, city, sizes, and questionnaire answers.

### Request Body
```json
{
  "country": "PK",
  "city": "Lahore",
  "plot_size_sqft": 1250,
  "covered_area_sqft": 1100,
  "answers": {
    "structure_type": "finished",
    "drawing_required": "yes",
    "brick_type": "awal",
    "foundation_depth": "3ft",
    "termite_spray": "yes",
    "steel_type": "branded",
    "bore": "no",
    "electric_pipe": "branded",
    "sanitary_pipe": "local",
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

### Request Field Definitions
| Field | Type | Required | Description |
|---|---|---|---|
| `country` | String | Yes | Country code |
| `city` | String | Yes | City name or mapped city id |
| `plot_size_sqft` | Number | Yes | Plot size in square feet |
| `covered_area_sqft` | Number | Yes | Covered area in square feet |
| `answers` | Object | Yes | Key-value map of questionnaire answers |

### Response Shape
```json
{
  "currency": "PKR",
  "line_items": [
    {
      "material": "Foundation",
      "qty": 1650,
      "unit": "sqft",
      "rate": 100,
      "cost": 165000,
      "category": "gray"
    }
  ],
  "totals": {
    "subtotal": 165000,
    "grand_total": 165000
  },
  "phase_breakdown": {
    "gray": {
      "subtotal": 165000,
      "line_items": []
    },
    "finish": {
      "subtotal": 0,
      "line_items": []
    }
  }
}
```

### Response Field Definitions
| Field | Type | Description |
|---|---|---|
| `currency` | String | `PKR` for Pakistan, `INR` for India |
| `line_items` | Array | Full flattened item list |
| `line_items[].material` | String | Material or cost component name |
| `line_items[].qty` | Number | Quantity consumed or computed quantity |
| `line_items[].unit` | String | Unit for quantity |
| `line_items[].rate` | Number | Rate applied |
| `line_items[].cost` | Number | Final cost for the item |
| `line_items[].category` | String | Logical estimate category, for example `gray`, `finish`, `combined` |
| `totals.subtotal` | Number | Sum before any future additions such as taxes or fees |
| `totals.grand_total` | Number | Final total payable/estimated amount |
| `phase_breakdown` | Object | Optional detailed totals by phase |

V1 note:
- Pakistan may return `gray` and `finish` phase totals.
- India V1 may return a single combined set of line items with no finish/gray split.

---

## Standard Validation Rules

### Common Input Rules
| Field | Rule |
|---|---|
| `country` | Must be one of supported country codes |
| `city` | Must exist for selected country |
| `plot_size_sqft` | Must be a positive number |
| `covered_area_sqft` | Must be a positive number |
| `answers` | Must be an object |

### Pakistan Answer Rules
- All required keys for visible steps must be present.
- Keys must match the allowed questionnaire keys exactly.
- Values must match the allowed option values exactly.
- Finish answers must only be sent when `structure_type = finished`.

### India Rules
- No extra questionnaire answers are required in V1.
- If `answers` is omitted or empty, backend uses default assumptions.

---

## Error Handling
The backend should return a consistent JSON error format.

### Standard Error Response
```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Invalid request payload.",
    "details": [
      {
        "field": "city",
        "issue": "City is required."
      }
    ]
  }
}
```

### Error Codes
| Code | When to Use |
|---|---|
| `VALIDATION_ERROR` | Missing fields, invalid types, invalid sizes |
| `INVALID_COUNTRY` | Country code not supported |
| `INVALID_CITY` | City not found for selected country |
| `INVALID_ANSWER_KEY` | Unknown answer key provided |
| `INVALID_ANSWER_VALUE` | Answer value not allowed for a known key |
| `QUESTIONNAIRE_NOT_FOUND` | Questionnaire flow missing for country/city/flow |
| `RATES_NOT_FOUND` | Material or city rates missing |
| `ESTIMATE_CALCULATION_FAILED` | Unexpected calculation failure |

### Required Error Cases

#### Missing Country or City
Example:
```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Invalid request payload.",
    "details": [
      { "field": "country", "issue": "Country is required." },
      { "field": "city", "issue": "City is required." }
    ]
  }
}
```

#### Invalid Answer Key
Example:
```json
{
  "error": {
    "code": "INVALID_ANSWER_KEY",
    "message": "One or more answer keys are not supported.",
    "details": [
      { "field": "answers.fake_key", "issue": "Unknown answer key." }
    ]
  }
}
```

#### Invalid Answer Value
Example:
```json
{
  "error": {
    "code": "INVALID_ANSWER_VALUE",
    "message": "One or more answer values are invalid.",
    "details": [
      { "field": "answers.foundation_depth", "issue": "Allowed values are 3ft, 4ft." }
    ]
  }
}
```

#### Rates Not Found
Example:
```json
{
  "error": {
    "code": "RATES_NOT_FOUND",
    "message": "Rates were not found for the selected city.",
    "details": [
      { "field": "city", "issue": "No rate configuration exists for Lahore." }
    ]
  }
}
```

Flutter guidance:
- Always display `error.message`.
- If `details` is present, attach field-specific messages to form inputs where possible.

---

## Quick Integration Guide
This is the recommended Flutter integration sequence.

### App Startup or Estimate Screen Entry
1. Call `GET /v1/meta/countries`.
2. Store the selected country code.
3. After country selection, call `GET /v1/meta/cities?country=<code>`.
4. Store the selected city.

### Before Rendering Pakistan Questions
1. Call `GET /v1/questionnaire?country=PK&city=<city>&flow=house_estimate_v1`.
2. Cache the returned flow object in memory for the current estimate session.
3. Render steps in order.
4. Apply visibility rules on the client.
5. Save answers in a flat map using the exact backend keys.

### Before Rendering India Estimate Form
1. Country is `IN`.
2. City is selected.
3. No questionnaire call is strictly required for V1 if the UI is fixed and minimal.
4. If the app architecture is fully dynamic, the questionnaire endpoint can still be called and may return a minimal flow.

### When to Call Estimate
Call `POST /v1/estimate` when:
- Country is selected
- City is selected
- Plot size is entered
- Covered area is entered
- For Pakistan, all required visible questions are answered

### What Flutter Should Store
Store these values locally for the current session:
- `country`
- `city`
- `plot_size_sqft`
- `covered_area_sqft`
- `answers` object
- Questionnaire response for rendering and validation

### Recommended Client Validation
Before calling estimate:
- Verify numeric fields are not empty
- Verify values are positive
- Verify all required visible answers are selected
- Remove finish answers if `structure_type = gray`

---

## Pakistan Full Example

### Example Request
```json
{
  "country": "PK",
  "city": "Lahore",
  "plot_size_sqft": 1250,
  "covered_area_sqft": 1100,
  "answers": {
    "structure_type": "finished",
    "drawing_required": "yes",
    "brick_type": "awal",
    "foundation_depth": "3ft",
    "termite_spray": "yes",
    "steel_type": "branded",
    "bore": "no",
    "electric_pipe": "branded",
    "sanitary_pipe": "local",
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

### Example Response
```json
{
  "currency": "PKR",
  "line_items": [
    {
      "material": "Foundation",
      "qty": 1875,
      "unit": "sqft",
      "rate": 100,
      "cost": 187500,
      "category": "gray"
    },
    {
      "material": "Termite Spray",
      "qty": 1250,
      "unit": "sqft",
      "rate": 5,
      "cost": 6250,
      "category": "gray"
    },
    {
      "material": "Ent (Bricks)",
      "qty": 3300,
      "unit": "pcs",
      "rate": 15,
      "cost": 49500,
      "category": "gray"
    },
    {
      "material": "Saria (Steel)",
      "qty": 1100,
      "unit": "kgs",
      "rate": 280,
      "cost": 308000,
      "category": "gray"
    },
    {
      "material": "Floor (Marble)",
      "qty": 1100,
      "unit": "m",
      "rate": 150,
      "cost": 165000,
      "category": "finish"
    },
    {
      "material": "Electrical Wiring",
      "qty": 1100,
      "unit": "sqft",
      "rate": 55,
      "cost": 60500,
      "category": "finish"
    },
    {
      "material": "Paint",
      "qty": 1100,
      "unit": "sqft",
      "rate": 90,
      "cost": 99000,
      "category": "finish"
    }
  ],
  "totals": {
    "subtotal": 875750,
    "grand_total": 875750
  },
  "phase_breakdown": {
    "gray": {
      "subtotal": 551250,
      "line_items": [
        {
          "material": "Foundation",
          "qty": 1875,
          "unit": "sqft",
          "rate": 100,
          "cost": 187500,
          "category": "gray"
        },
        {
          "material": "Termite Spray",
          "qty": 1250,
          "unit": "sqft",
          "rate": 5,
          "cost": 6250,
          "category": "gray"
        },
        {
          "material": "Ent (Bricks)",
          "qty": 3300,
          "unit": "pcs",
          "rate": 15,
          "cost": 49500,
          "category": "gray"
        },
        {
          "material": "Saria (Steel)",
          "qty": 1100,
          "unit": "kgs",
          "rate": 280,
          "cost": 308000,
          "category": "gray"
        }
      ]
    },
    "finish": {
      "subtotal": 324500,
      "line_items": [
        {
          "material": "Floor (Marble)",
          "qty": 1100,
          "unit": "m",
          "rate": 150,
          "cost": 165000,
          "category": "finish"
        },
        {
          "material": "Electrical Wiring",
          "qty": 1100,
          "unit": "sqft",
          "rate": 55,
          "cost": 60500,
          "category": "finish"
        },
        {
          "material": "Paint",
          "qty": 1100,
          "unit": "sqft",
          "rate": 90,
          "cost": 99000,
          "category": "finish"
        }
      ]
    }
  }
}
```

Notes:
- Example numbers are illustrative.
- Actual numbers depend on backend rules and configured rates.
- The Flutter app should render all returned line items and totals without recalculating costs locally.

---

## India Full Example

### Example Request
```json
{
  "country": "IN",
  "city": "Mumbai",
  "plot_size_sqft": 1000,
  "covered_area_sqft": 900,
  "answers": {}
}
```

### Example Response
```json
{
  "currency": "INR",
  "line_items": [
    {
      "material": "Cement",
      "qty": 108,
      "unit": "Bags",
      "rate": 400,
      "cost": 43200,
      "category": "combined"
    },
    {
      "material": "Steel TMT",
      "qty": 72,
      "unit": "KGs",
      "rate": 65,
      "cost": 4680,
      "category": "combined"
    },
    {
      "material": "Sand",
      "qty": 45,
      "unit": "cft",
      "rate": 55,
      "cost": 2475,
      "category": "combined"
    },
    {
      "material": "Aggregate",
      "qty": 36,
      "unit": "cft",
      "rate": 45,
      "cost": 1620,
      "category": "combined"
    }
  ],
  "totals": {
    "subtotal": 51975,
    "grand_total": 51975
  }
}
```

Notes:
- India V1 uses backend default assumptions.
- No extra answers are required in V1.
- If future India versions add questions, the questionnaire endpoint can be extended without breaking this base flow.

---

## Backend Responsibilities
The backend is responsible for:
- Validating all request fields
- Validating answer keys and values
- Resolving questionnaire visibility and allowed inputs
- Applying impacts from selected answers
- Calculating material consumption from formulas
- Resolving rates from city/country configuration
- Returning itemized cost breakdown
- Returning normalized error responses

The backend should be treated as the single source of truth for estimate logic.

---

## Flutter Responsibilities
The Flutter app is responsible for:
- Calling metadata endpoints in sequence
- Rendering questionnaire steps in backend order
- Respecting conditional visibility rules
- Sending exact backend keys and option values
- Performing lightweight client-side validation
- Displaying returned line items and totals
- Handling API error states gracefully

The Flutter app should not:
- Recalculate backend totals
- Hardcode pricing logic
- Infer hidden questionnaire rules outside the API contract

---

## Recommended V1 Implementation Notes
- Keep answer storage as a flat `Map<String, dynamic>` in Flutter.
- Send only visible and selected question keys.
- For Pakistan gray-only estimates, omit finish answers.
- For Pakistan finished estimates, include both gray and finish answers.
- For India V1, send an empty `answers` object if no dynamic inputs are needed.
- Use backend response categories to group UI sections such as Gray and Finish.

---

## Summary
V1 integration should follow this sequence:
1. Fetch countries
2. Fetch cities
3. Fetch questionnaire for Pakistan flows
4. Collect house info and answers
5. Call estimate
6. Render itemized result and totals

This contract is intentionally practical and V1-focused so Flutter and backend can move independently while using a stable payload structure.
