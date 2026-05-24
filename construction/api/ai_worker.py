# AI worker - runs as a Frappe RQ background job.
#
# Vendor dispatch is configured in AI Settings (per tool_id child row):
#   floor_plan -> default: gemini (gemini-2.0-flash, vision JSON)
#   interior/exterior/garden/layout/cleanup/ref/paint/replace/floor
#              -> default: mock (returns a deterministic stock image).
#                 Flip the row's vendor to "gemini" + model
#                 "gemini-2.5-flash-image" once a Gemini key is set on
#                 AI Settings.
#
# Each vendor adapter MUST set:
#   doc.model, doc.tokens_input, doc.tokens_output, doc.cost_cents
# and store the result_json with shape:
#   floor_plan: {"ai_result": {...}, "estimate": null}
#   image-edit: {"images": [{"url", "thumb_url", "seed"}, ...], "prompt"}
# cost_cents drives budget enforcement in v1._check_budgets.

import base64
import io
import json
import time
import traceback

import frappe
import requests
from frappe.utils import now_datetime

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
        tool_id = doc.job_type

        if tool_id == "floor_plan":
            result = _run_floor_plan(doc, payload)
        elif tool_id in _IMAGE_EDIT_TOOLS:
            result = _run_image_edit(doc, payload, tool_id)
        else:
            raise ValueError(f"Unknown job_type: {tool_id}")

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
        doc.error_message = "Worker failed - see error log"
        doc.status = "failed"

    doc.completed_at = now_datetime()
    doc.save(ignore_permissions=True)
    frappe.db.commit()

    # PREM-4: failed jobs auto-refund the credits that were debited at submit
    # time. Idempotent: _refund_credits keys on the original (ref_doctype,
    # ref_name) pair so re-running this is safe.
    if doc.status == "failed":
        try:
            from construction.api.v1 import _refund_credits, _credit_gating_enabled
            if _credit_gating_enabled():
                _refund_credits(
                    doc.user,
                    ref_doctype="AI Job",
                    ref_name=doc.name,
                    reason=f"AI job failed: {doc.error_code or 'unknown'}",
                )
                frappe.db.commit()
        except Exception:
            frappe.log_error(
                traceback.format_exc(), f"ai_worker.refund_failed:{name}"
            )


class _AIDisabled(Exception):
    pass


class _VendorError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


_IMAGE_EDIT_TOOLS = {
    "interior", "exterior", "garden", "layout",
    "cleanup", "ref", "paint", "replace", "floor",
}


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------

def _read_uploaded_file(image_url):
    """Resolve a Frappe file_url (/private/files/foo.png or /files/foo.png)
    to (bytes, mime_type)."""
    if not image_url:
        return None, None
    import os
    from frappe.utils.file_manager import get_file_path
    try:
        path = get_file_path(image_url)
    except Exception:
        path = None
    if not path or not os.path.exists(path):
        # Try reading the File doc directly as a fallback.
        files = frappe.get_all(
            "File",
            filters={"file_url": image_url},
            fields=["name", "file_name"],
            limit=1,
        )
        if not files:
            raise _VendorError("FILE_MISSING", f"Could not load uploaded file {image_url}")
        file_doc = frappe.get_doc("File", files[0]["name"])
        return file_doc.get_content(), _guess_mime(file_doc.file_name)

    with open(path, "rb") as f:
        data = f.read()
    return data, _guess_mime(path)


def _guess_mime(filename):
    f = (filename or "").lower()
    if f.endswith(".png"):
        return "image/png"
    if f.endswith(".webp"):
        return "image/webp"
    return "image/jpeg"


def _save_result_image(name_prefix, content, mime="image/png"):
    """Save bytes as a private Frappe File and return its file_url."""
    from frappe.utils.file_manager import save_file
    ext = "png" if mime == "image/png" else ("webp" if mime == "image/webp" else "jpg")
    file_doc = save_file(
        fname=f"{name_prefix}.{ext}",
        content=content,
        dt=None,
        dn=None,
        is_private=1,
    )
    return file_doc.file_url


def _require_key(vendor):
    key = ai_cfg.get_api_key(vendor)
    if not key:
        raise _AIDisabled(f"No API key configured for vendor {vendor}")
    return key


# ---------------------------------------------------------------------------
# Floor plan (Gemini 2.0 Flash vision JSON)
# ---------------------------------------------------------------------------

_FLOOR_PLAN_PROMPT = (
    "You are an architect analyzing a residential floor plan image. "
    "Identify the total covered area in square feet, the overall plot dimensions "
    "as a 'A x B ft' string, and the count of distinct rooms. "
    "Reply ONLY with this JSON: "
    "{\"detected_area_sqft\": number, \"detected_dimensions\": string, "
    "\"detected_rooms\": integer, \"confidence\": number_between_0_and_1, "
    "\"confidence_level\": one of [\"High\",\"Medium\",\"Low\"], "
    "\"notes\": string}"
)


def _run_floor_plan(doc, payload):
    cfg = ai_cfg.get_job_config("floor_plan")
    vendor = (cfg.get("vendor") or "gemini").lower()
    model = cfg.get("model") or "gemini-2.0-flash"

    image_bytes, mime = _read_uploaded_file(payload.get("image_url"))

    if vendor == "mock":
        doc.model = "mock-floor-plan"
        doc.tokens_input = 0
        doc.tokens_output = 0
        doc.cost_cents = 0
        return _mock_floor_plan_response()

    if vendor == "gemini":
        text, usage = _gemini_vision(model, _FLOOR_PLAN_PROMPT, image_bytes, mime)
        doc.model = model
        doc.tokens_input = usage.get("input_tokens", 0)
        doc.tokens_output = usage.get("output_tokens", 0)
        doc.cost_cents = max(
            1,
            int(round(
                (doc.tokens_input * 0.10 / 1_000_000
                 + doc.tokens_output * 0.40 / 1_000_000) * 100
            )),
        )
        return _parse_floor_plan_response(text)

    raise _AIDisabled(f"Floor-plan vendor {vendor} is not implemented yet.")


def _gemini_vision(model, prompt, image_bytes, mime):
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
    try:
        ai = json.loads(text)
    except json.JSONDecodeError:
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
        "estimate": None,
    }


def _mock_floor_plan_response():
    return {
        "ai_result": {
            "detected_area_sqft": 1450,
            "detected_dimensions": "50 x 29 ft",
            "detected_rooms": 7,
            "confidence": 0.86,
            "confidence_level": "High",
            "notes": "Mock response - no real analysis performed.",
        },
        "estimate": None,
    }


# ---------------------------------------------------------------------------
# Image edit (Gemini 2.5 Flash Image / Nano Banana)
# ---------------------------------------------------------------------------

# Per-tool prompt templates. Each receives a dict with style, room, color, notes
# (any may be None) and returns a single string prompt.

def _prompt_interior(p):
    parts = [
        f"Photorealistic interior redesign of this {p.get('room') or 'living room'}",
        f"in {p.get('style') or 'modern'} style",
    ]
    if p.get("color"):
        parts.append(f"with a {p['color']} color palette")
    if p.get("notes"):
        parts.append(p["notes"])
    parts.append(
        "Preserve the original room geometry, window positions and ceiling lines. "
        "Replace only furniture, finishes and decor. Magazine quality, natural lighting."
    )
    return ", ".join(parts)


def _prompt_exterior(p):
    parts = [
        "Photorealistic exterior redesign of this building facade",
        f"in {p.get('style') or 'modern'} style",
    ]
    if p.get("color"):
        parts.append(f"with a {p['color']} color palette")
    if p.get("notes"):
        parts.append(p["notes"])
    parts.append(
        "Preserve the original building footprint, roof line, and window/door positions. "
        "Change only cladding, paint, and exterior trim. Daylight, photoreal."
    )
    return ", ".join(parts)


def _prompt_garden(p):
    parts = [
        "Photorealistic landscape redesign of this outdoor area",
        f"in {p.get('style') or 'tropical'} style",
    ]
    if p.get("color"):
        parts.append(f"with a {p['color']} color tone")
    if p.get("notes"):
        parts.append(p["notes"])
    parts.append(
        "Keep the property boundary, hardscape paths and building edges unchanged. "
        "Replace only planting, ground cover and outdoor furniture. Bright daylight."
    )
    return ", ".join(parts)


def _prompt_layout(p):
    parts = [
        f"Rearrange the furniture in this {p.get('room') or 'room'} for better balance and flow",
        f"keep the {p.get('style') or 'existing'} style",
    ]
    if p.get("notes"):
        parts.append(p["notes"])
    parts.append(
        "Do not change wall colors, flooring or fixtures. Photorealistic, same camera angle."
    )
    return ", ".join(parts)


def _prompt_cleanup(p):
    parts = [
        "Remove all clutter, personal items and visible cables from this scene",
        "leave a clean, staged version of the same room",
    ]
    if p.get("notes"):
        parts.append(p["notes"])
    parts.append("Preserve furniture, finishes and lighting exactly.")
    return ", ".join(parts)


def _prompt_ref(p):
    parts = [
        "Apply the visual style of the provided reference image to this room",
        f"keeping the {p.get('room') or 'room'} layout intact",
    ]
    if p.get("notes"):
        parts.append(p["notes"])
    parts.append("Photorealistic, magazine-quality result.")
    return ", ".join(parts)


def _prompt_paint(p):
    parts = ["Repaint this scene"]
    if p.get("color"):
        parts.append(f"with a {p['color']} palette")
    if p.get("style"):
        parts.append(f"matching a {p['style']} aesthetic")
    if p.get("notes"):
        parts.append(p["notes"])
    parts.append(
        "Change only paint and visible wall/ceiling color. Preserve furniture, "
        "flooring, fixtures, and natural light."
    )
    return ", ".join(parts)


def _prompt_replace(p):
    parts = ["Replace the masked object in this image"]
    if p.get("notes"):
        parts.append(f"with: {p['notes']}")
    elif p.get("style"):
        parts.append(f"with a {p['style']}-style alternative")
    parts.append(
        "Match the room lighting and perspective. Keep everything outside the mask unchanged."
    )
    return ", ".join(parts)


def _prompt_floor(p):
    parts = ["Replace only the flooring in this room"]
    if p.get("style"):
        parts.append(f"with a {p['style']} finish")
    if p.get("color"):
        parts.append(f"in {p['color']} tone")
    if p.get("notes"):
        parts.append(p["notes"])
    parts.append(
        "Keep walls, ceiling, furniture and fixtures unchanged. Photorealistic, correct reflections."
    )
    return ", ".join(parts)


_PROMPT_BUILDERS = {
    "interior": _prompt_interior,
    "exterior": _prompt_exterior,
    "garden":   _prompt_garden,
    "layout":   _prompt_layout,
    "cleanup":  _prompt_cleanup,
    "ref":      _prompt_ref,
    "paint":    _prompt_paint,
    "replace":  _prompt_replace,
    "floor":    _prompt_floor,
}


def _run_image_edit(doc, payload, tool_id):
    cfg = ai_cfg.get_job_config(tool_id)
    vendor = (cfg.get("vendor") or "mock").lower()
    model = cfg.get("model") or "gemini-2.5-flash-image"

    image_bytes, mime = _read_uploaded_file(payload.get("image_url"))
    mask_bytes = mask_mime = None
    ref_bytes = ref_mime = None
    if payload.get("mask_url"):
        mask_bytes, mask_mime = _read_uploaded_file(payload["mask_url"])
    if payload.get("ref_image_url"):
        ref_bytes, ref_mime = _read_uploaded_file(payload["ref_image_url"])

    builder = _PROMPT_BUILDERS.get(tool_id)
    if not builder:
        raise _VendorError("UNKNOWN_TOOL", f"No prompt builder for tool {tool_id}")
    prompt = builder({
        "style": payload.get("style"),
        "room":  payload.get("room"),
        "color": payload.get("color"),
        "notes": payload.get("notes"),
    })

    variations = int(doc.variation_count or 1)
    hd = (doc.quality or "std").lower() == "hd"

    if vendor == "mock":
        doc.model = "mock-image-edit"
        doc.cost_cents = 0
        return _mock_image_edit(tool_id, payload, variations, prompt, hd)

    if vendor == "gemini":
        return _gemini_image_edit(
            doc, model, prompt, image_bytes, mime,
            mask_bytes, mask_mime, ref_bytes, ref_mime, variations, hd
        )

    if vendor == "fal":
        raise _AIDisabled("fal.ai adapter not implemented yet - set vendor to gemini or mock.")

    raise _AIDisabled(f"Image-edit vendor {vendor} is not implemented yet.")


def _gemini_image_edit(doc, model, prompt, image_bytes, mime,
                       mask_bytes, mask_mime, ref_bytes, ref_mime,
                       variations, hd):
    """Call Gemini 2.5 Flash Image up to [variations] times in parallel.

    Returns the worker result dict {"images": [...], "prompt": str, "model": str}.
    """
    api_key = _require_key("gemini")
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={api_key}"
    )

    parts = [{"text": prompt}]
    if mask_bytes:
        parts.append({"text": "The white pixels in the next image are the mask region to edit; ignore everything else."})
        parts.append({"inline_data": {"mime_type": mask_mime or "image/png",
                                       "data": base64.b64encode(mask_bytes).decode("ascii")}})
    if ref_bytes:
        parts.append({"text": "Use the next image as a style reference."})
        parts.append({"inline_data": {"mime_type": ref_mime or "image/jpeg",
                                       "data": base64.b64encode(ref_bytes).decode("ascii")}})
    parts.append({"inline_data": {"mime_type": mime or "image/jpeg",
                                   "data": base64.b64encode(image_bytes).decode("ascii")}})

    body_template = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "temperature": 0.8,
            "responseModalities": ["IMAGE"],
        },
    }

    from concurrent.futures import ThreadPoolExecutor

    def _call_once(idx):
        body = json.loads(json.dumps(body_template))
        body["generationConfig"]["seed"] = 1000 + idx * 17
        try:
            resp = requests.post(url, json=body, timeout=120)
        except requests.RequestException as e:
            raise _VendorError("VENDOR_NETWORK", f"Gemini request failed: {e}")
        if resp.status_code != 200:
            raise _VendorError(
                "VENDOR_HTTP",
                f"Gemini returned {resp.status_code}: {resp.text[:300]}",
            )
        return resp.json()

    results = []
    with ThreadPoolExecutor(max_workers=min(4, variations)) as ex:
        for r in ex.map(_call_once, range(variations)):
            results.append(r)

    images = []
    total_tokens_in = 0
    total_tokens_out = 0
    for i, data in enumerate(results):
        try:
            cand = data["candidates"][0]
            inline = None
            for p in cand["content"]["parts"]:
                if "inline_data" in p:
                    inline = p["inline_data"]
                    break
                if "inlineData" in p:
                    inline = p["inlineData"]
                    break
            if not inline:
                raise _VendorError("VENDOR_RESPONSE", f"Gemini response had no image part: {data}")
            img_bytes = base64.b64decode(inline.get("data") or inline.get("data_b64") or "")
            img_mime = inline.get("mime_type") or inline.get("mimeType") or "image/png"
            url_saved = _save_result_image(f"ai_{doc.name}_v{i}", img_bytes, img_mime)
            images.append({"url": url_saved, "thumb_url": url_saved, "seed": 1000 + i * 17})
        except (KeyError, IndexError):
            raise _VendorError("VENDOR_RESPONSE", f"Unexpected Gemini shape: {data}")
        usage = data.get("usageMetadata") or {}
        total_tokens_in += int(usage.get("promptTokenCount", 0))
        total_tokens_out += int(usage.get("candidatesTokenCount", 0))

    doc.model = model
    doc.tokens_input = total_tokens_in
    doc.tokens_output = total_tokens_out
    # Gemini 2.5 Flash Image lists at ~$0.039/image. Add 50% for HD as a buffer
    # (Google may bill more for larger outputs).
    per_image_cents = int(round(3.9 * (1.5 if hd else 1.0)))
    doc.cost_cents = max(1, per_image_cents * len(images))

    return {"images": images, "prompt": prompt, "model": model, "hd": hd}


def _mock_image_edit(tool_id, payload, variations, prompt, hd):
    """Return [variations] stock placeholder image URLs.

    The mock uses placehold.co (a public placeholder service). When you
    flip the vendor to 'gemini' on a real key, these are replaced with
    actual Nano Banana outputs without any other code change.
    """
    style = (payload.get("style") or "mock").replace(" ", "+")
    size = "2048x2048" if hd else "1024x1024"
    images = []
    palette = ["#1f2937", "#374151", "#4b5563", "#6b7280"]
    for i in range(variations):
        bg = palette[i % len(palette)].lstrip("#")
        url = f"https://placehold.co/{size}/{bg}/F4EFE6?text={tool_id}+%7C+{style}+%7C+V{i+1}"
        images.append({"url": url, "thumb_url": url, "seed": 1000 + i * 17})
    return {"images": images, "prompt": prompt, "model": "mock-image-edit", "hd": hd}


# ---------------------------------------------------------------------------
# Legacy fal.ai stub kept only for back-compat with the old interior endpoint.
# Returns a stock placeholder until a fal adapter is wired.
# ---------------------------------------------------------------------------

def _run_interior_design(doc, payload):
    """Legacy entrypoint - the old ai_interior_generate endpoint still uses
    this. Internally we now route via _run_image_edit with tool_id=interior."""
    return _run_image_edit(doc, payload, "interior")
