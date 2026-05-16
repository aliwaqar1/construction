# AI worker — runs as a Frappe RQ background job.
#
# Vendor dispatch is configured in the AI Settings doctype (per job_type).
#   floor_plan      -> default: gemini  (gemini-2.0-flash, vision)
#   interior_design -> default: fal.ai  (fal-ai/flux/schnell, txt2img)
#
# Each vendor adapter MUST set:
#   doc.model, doc.tokens_input, doc.tokens_output, doc.cost_cents
# before returning. cost_cents drives budget enforcement in v1._check_budgets.

import base64
import json
import time
import traceback

import frappe
import requests
from frappe.utils import get_files_path, now_datetime

from construction.construction.doctype.ai_settings import ai_settings as ai_cfg


def run_job(name):
    """Entrypoint invoked by frappe.enqueue."""
    try:
        doc = frappe.get_doc("AI Job", name)
    except Exception:
        frappe.log_error(traceback.format_exc(), f"ai_worker.load_failed:{name}")
        return

    doc.status = "running"
    doc.started_at = now_datetime()
    doc.save(ignore_permissions=True)
    frappe.db.commit()

    try:
        payload = json.loads(doc.request_payload_json or "{}")
        if doc.job_type == "floor_plan":
            result = _run_floor_plan(doc, payload)
        elif doc.job_type == "interior_design":
            result = _run_interior_design(doc, payload)
        else:
            raise ValueError(f"Unknown job_type: {doc.job_type}")

        doc.result_json = json.dumps(result)
        doc.status = "succeeded"
    except _AIDisabled as e:
        doc.error_code = "AI_DISABLED"
        doc.error_message = str(e)
        doc.status = "failed"
    except _VendorError as e:
        doc.error_code = e.code
        doc.error_message = str(e)
        doc.status = "failed"
    except Exception:
        frappe.log_error(traceback.format_exc(), f"ai_worker.run:{name}")
        doc.error_code = "WORKER_ERROR"
        doc.error_message = "Worker failed — see error log"
        doc.status = "failed"

    doc.completed_at = now_datetime()
    doc.save(ignore_permissions=True)
    frappe.db.commit()


class _AIDisabled(Exception):
    pass


class _VendorError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


# ── Helpers ────────────────────────────────────────────────────────────────

def _read_uploaded_file(image_url):
    """Resolve a Frappe file_url ('/private/files/foo.png' or '/files/foo.png')
    to (bytes, mime_type)."""
    if not image_url:
        raise _VendorError("MISSING_IMAGE", "No image was uploaded with the request.")

    rows = frappe.get_all(
        "File",
        filters={"file_url": image_url},
        fields=["name", "is_private", "file_name"],
        limit=1,
    )
    if not rows:
        raise _VendorError("MISSING_IMAGE", f"File {image_url} not found.")

    is_private = bool(rows[0].get("is_private"))
    fname = rows[0].get("file_name") or ""
    base = get_files_path(is_private=is_private)
    full = f"{base}/{fname}"
    try:
        with open(full, "rb") as f:
            data = f.read()
    except OSError as e:
        raise _VendorError("MISSING_IMAGE", f"Could not read {full}: {e}")

    mime = "image/jpeg"
    low = fname.lower()
    if low.endswith(".png"):
        mime = "image/png"
    elif low.endswith(".webp"):
        mime = "image/webp"
    elif low.endswith(".gif"):
        mime = "image/gif"
    return data, mime


def _require_key(vendor):
    key = ai_cfg.get_api_key(vendor)
    if not key:
        raise _AIDisabled(
            f"{vendor} API key is not configured. Set it in AI Settings."
        )
    return key


# ── Floor plan (Google Gemini Flash, vision → JSON) ────────────────────────

_FLOOR_PLAN_PROMPT = """You are an assistant that extracts structured data from a residential floor plan image.

Inspect the floor plan and respond with ONLY a JSON object — no prose, no markdown — matching this schema:

{
  "detected_area_sqft": <integer total covered area in square feet>,
  "detected_dimensions": "<string e.g. '45ft x 40ft'>",
  "detected_rooms": <integer total number of rooms incl. bedrooms, kitchen, bathrooms, living, dining>,
  "confidence": <float 0..1>,
  "confidence_level": "<one of: High, Medium, Low>",
  "notes": "<one short sentence about anything unusual or unclear>"
}

Confidence rules:
- High (>=0.85): all dimensions clearly labeled, layout unambiguous.
- Medium (0.50..0.84): some values inferred from scale or partial labels.
- Low (<0.50): significant guesswork; the image is unclear or not a residential floor plan.
"""


def _run_floor_plan(doc, payload):
    cfg = ai_cfg.get_job_config("floor_plan")
    vendor = (cfg.get("vendor") or "gemini").lower()
    model = cfg.get("model") or "gemini-2.0-flash"

    image_bytes, mime = _read_uploaded_file(payload.get("image_url"))

    if vendor == "gemini":
        text, usage = _gemini_vision(model, _FLOOR_PLAN_PROMPT, image_bytes, mime)
        doc.model = model
        doc.tokens_input = usage.get("input_tokens", 0)
        doc.tokens_output = usage.get("output_tokens", 0)
        # Gemini 2.0 Flash pricing (rough): $0.10/M input, $0.40/M output tokens.
        # Vision adds ~258 tokens per image; included in input count.
        doc.cost_cents = max(
            1,
            int(round(
                (doc.tokens_input * 0.10 / 1_000_000
                 + doc.tokens_output * 0.40 / 1_000_000) * 100
            )),
        )
        return _parse_floor_plan_response(text)

    raise _AIDisabled(f"Floor-plan vendor '{vendor}' is not implemented yet.")


def _gemini_vision(model, prompt, image_bytes, mime):
    """Call Google Gemini multi-modal endpoint. Returns (text, usage)."""
    api_key = _require_key("gemini")
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={api_key}"
    )
    body = {
        "contents": [{
            "role": "user",
            "parts": [
                {"text": prompt},
                {"inline_data": {
                    "mime_type": mime,
                    "data": base64.b64encode(image_bytes).decode("ascii"),
                }},
            ],
        }],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
        },
    }
    try:
        resp = requests.post(url, json=body, timeout=60)
    except requests.RequestException as e:
        raise _VendorError("VENDOR_NETWORK", f"Gemini request failed: {e}")
    if resp.status_code != 200:
        raise _VendorError(
            "VENDOR_HTTP",
            f"Gemini returned {resp.status_code}: {resp.text[:300]}",
        )
    data = resp.json()
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        raise _VendorError("VENDOR_RESPONSE", f"Unexpected Gemini shape: {data}")
    usage = data.get("usageMetadata") or {}
    return text, {
        "input_tokens": usage.get("promptTokenCount", 0),
        "output_tokens": usage.get("candidatesTokenCount", 0),
    }


def _parse_floor_plan_response(text):
    """Parse the LLM JSON output into the FRD §6.5 shape."""
    try:
        ai = json.loads(text)
    except json.JSONDecodeError:
        # Last-ditch: extract the first {...} block.
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1:
            raise _VendorError("VENDOR_RESPONSE", "AI did not return JSON.")
        ai = json.loads(text[start : end + 1])

    return {
        "ai_result": {
            "detected_area_sqft": ai.get("detected_area_sqft", 0),
            "detected_dimensions": ai.get("detected_dimensions", ""),
            "detected_rooms": ai.get("detected_rooms", 0),
            "confidence": ai.get("confidence", 0),
            "confidence_level": ai.get("confidence_level", "Low"),
            "notes": ai.get("notes", ""),
        },
        "estimate": None,  # Compute via /v1.estimate after user confirms values.
    }


# ── Interior design (fal.ai FLUX-schnell, txt2img) ─────────────────────────

def _run_interior_design(doc, payload):
    cfg = ai_cfg.get_job_config("interior_design")
    vendor = (cfg.get("vendor") or "fal").lower()
    model = cfg.get("model") or "fal-ai/flux/schnell"

    prompt = _build_interior_prompt(payload)

    if vendor == "fal":
        result, cost_cents = _fal_run(model, prompt)
        doc.model = model
        doc.cost_cents = cost_cents
        return result

    raise _AIDisabled(f"Interior vendor '{vendor}' is not implemented yet.")


def _build_interior_prompt(payload):
    parts = [
        f"Photorealistic interior design of a {payload.get('room_type', 'living room')}",
        f"in {payload.get('style', 'modern')} style",
    ]
    if payload.get("color_palette"):
        parts.append(f"with {payload['color_palette']} colour palette")
    if payload.get("notes"):
        parts.append(payload["notes"])
    parts.append("high detail, natural lighting, magazine quality")
    return ", ".join(parts)


def _fal_run(model, prompt):
    """Submit + poll fal.ai. Returns (result_dict, cost_cents)."""
    api_key = _require_key("fal")
    headers = {"Authorization": f"Key {api_key}"}

    # Submit
    submit_url = f"https://queue.fal.run/{model}"
    try:
        resp = requests.post(
            submit_url,
            headers=headers,
            json={"prompt": prompt, "image_size": "square_hd", "num_images": 1},
            timeout=60,
        )
    except requests.RequestException as e:
        raise _VendorError("VENDOR_NETWORK", f"fal.ai request failed: {e}")
    if resp.status_code not in (200, 202):
        raise _VendorError(
            "VENDOR_HTTP",
            f"fal.ai returned {resp.status_code}: {resp.text[:300]}",
        )
    submission = resp.json()
    request_id = submission.get("request_id")
    if not request_id:
        raise _VendorError("VENDOR_RESPONSE", "fal.ai submit missing request_id.")

    status_url = submission.get("status_url") or (
        f"https://queue.fal.run/{model}/requests/{request_id}/status"
    )
    response_url = submission.get("response_url") or (
        f"https://queue.fal.run/{model}/requests/{request_id}"
    )

    # Poll
    deadline = time.time() + 120
    while time.time() < deadline:
        try:
            s = requests.get(status_url, headers=headers, timeout=15)
        except requests.RequestException as e:
            raise _VendorError("VENDOR_NETWORK", f"fal.ai poll failed: {e}")
        sj = s.json() if s.status_code == 200 else {}
        if sj.get("status") in ("COMPLETED", "OK"):
            break
        if sj.get("status") in ("FAILED", "ERROR"):
            raise _VendorError("VENDOR_FAILED", f"fal.ai job failed: {sj}")
        time.sleep(1.5)
    else:
        raise _VendorError("VENDOR_TIMEOUT", "fal.ai did not finish in 120s.")

    # Fetch
    try:
        r = requests.get(response_url, headers=headers, timeout=30)
    except requests.RequestException as e:
        raise _VendorError("VENDOR_NETWORK", f"fal.ai fetch failed: {e}")
    if r.status_code != 200:
        raise _VendorError(
            "VENDOR_HTTP",
            f"fal.ai response returned {r.status_code}: {r.text[:300]}",
        )
    body = r.json()

    images = body.get("images") or []
    image_url = images[0]["url"] if images and isinstance(images[0], dict) else ""

    # FLUX-schnell on fal.ai: ~$0.003 per megapixel; 1024x1024 ≈ 1 MP.
    cost_cents = 1
    return (
        {
            "image_url": image_url,
            "suggestions": [],
            "palette": [],
            "seed": body.get("seed"),
        },
        cost_cents,
    )
