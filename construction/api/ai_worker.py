# AI worker - runs as a Frappe RQ background job.
#
# Vendor dispatch is configured in AI Settings (per tool_id child row):
#   floor_plan -> default: gemini (gemini-2.0-flash, vision JSON)
#   interior/exterior/garden/layout/cleanup/ref/paint/replace/floor
#              -> default: mock (returns a deterministic stock image).
#                 Flip the row's vendor to "gemini" + model
#                 "gemini-2.5-flash-image" once a Gemini key is set on
#                 AI Settings. "fal" (FLUX Kontext) is wired as a fallback
#                 vendor — set the row's vendor to "fal" + a fal model id.
#
# Each vendor adapter MUST set:
#   doc.model, doc.tokens_input, doc.tokens_output, doc.cost_cents
# and store the result_json with shape:
#   floor_plan: {"ai_result": {...}, "estimate": null}
#   image-edit: {"images": [{"url", "thumb_url", "seed"}, ...], "prompt"}
# cost_cents drives budget enforcement in v1._check_budgets.
#
# Error taxonomy (F2) — doc.error_code on failed jobs. error_message is always
# user-safe; technical detail goes to the Error Log via _VendorError.detail:
#   SAFETY_BLOCKED       vendor refused the content for safety (A3) — user must
#                        change photo/notes; NOT retryable, NOT a vendor outage.
#   MODERATION_REJECTED  our pre-flight gate rejected the source photo (A1).
#   VENDOR_RATE_LIMITED  vendor 429 after retries — transient.
#   VENDOR_UNAVAILABLE   vendor 5xx after retries — transient outage.
#   VENDOR_TIMEOUT       vendor request timed out after retries — transient.
#   VENDOR_NETWORK       connection-level failure after retries — transient.
#   VENDOR_HTTP          non-retryable vendor 4xx (bad key, bad request).
#   VENDOR_RESPONSE      2xx but malformed/unusable payload.
#   VENDOR_UNSUPPORTED   requested capability not available on this vendor.
#   FILE_MISSING         uploaded source file could not be read back.
#   AI_DISABLED          vendor/tool disabled or key missing.
#   WORKER_ERROR         unhandled exception — see Error Log.
# Transient codes feed the error-rate circuit breaker (B3): repeated vendor
# failures inside a short window auto-trip the kill switch.

import base64
import io
import json
import re
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

    if doc.status not in ("queued", "running"):
        # The watchdog (or a failed submit) already terminated and refunded
        # this job. Running it now would spend real vendor money on work the
        # user has been paid back for — and deliver an image they were told
        # they would not get, after they had already re-submitted at their own
        # cost.
        frappe.log_error(
            f"job {name} was already {doc.status}; skipping execution",
            "ai_worker.already_terminal",
        )
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
        _circuit_note_success(tool_id)
    except _AIDisabled as e:
        doc.error_code = "AI_DISABLED"
        doc.error_message = str(e)
        doc.status = "failed"
    except _VendorError as e:
        doc.error_code = e.code
        doc.error_message = str(e)
        doc.status = "failed"
        if e.detail:
            frappe.log_error(e.detail, f"ai_worker.vendor:{e.code}:{name}")
        _circuit_note_failure(doc.job_type, e.code)
    except Exception:
        frappe.log_error(traceback.format_exc(), f"ai_worker.run:{name}")
        doc.error_code = "WORKER_ERROR"
        doc.error_message = "Something went wrong on our side. Please try again."
        doc.status = "failed"
        _circuit_note_failure(doc.job_type, "WORKER_ERROR")

    doc.completed_at = now_datetime()
    doc.save(ignore_permissions=True)

    # Successful image jobs persist their first variation as an AI Design
    # server-side, so a paid result reaches My Designs even when no client is
    # watching (killed app, client poll timeout, backgrounded compare). Done
    # BEFORE the status commit so a client that sees "succeeded" is
    # guaranteed to find the row and its own save upserts instead of racing a
    # duplicate. Best-effort: a save hiccup must never fail a succeeded job.
    if doc.status == "succeeded" and doc.job_type in _IMAGE_EDIT_TOOLS:
        try:
            from construction.api.v1 import _upsert_design_for_job
            _upsert_design_for_job(doc, variation_index=0)
        except Exception:
            frappe.log_error(
                traceback.format_exc(), f"ai_worker.autosave:{name}"
            )

    frappe.db.commit()

    # PREM-4: failed jobs auto-refund the credits that were debited at submit
    # time. Idempotent: _refund_credits keys on the original (ref_doctype,
    # ref_name) pair so re-running this is safe.
    if doc.status == "failed":
        try:
            from construction.api.v1 import _refund_job_credits
            _refund_job_credits(
                doc.user,
                doc.name,
                doc.idempotency_key,
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
    """User-facing vendor failure.

    `message` is safe to show in the app verbatim; `detail` carries the
    technical payload (status codes, response bodies) and is only written to
    the server Error Log — never to doc.error_message (F2)."""

    def __init__(self, code, message, detail=None):
        super().__init__(message)
        self.code = code
        self.detail = detail


_IMAGE_EDIT_TOOLS = {
    "interior", "exterior", "garden", "layout",
    "cleanup", "ref", "paint", "replace", "floor",
}

# C2: generation modes for the image-edit tools. "photo" edits an uploaded
# photo (the default), "sketch" renders an uploaded hand-drawn sketch
# photorealistically, "text" creates from imagination with no source image at
# all. v1.ai_generate restricts text/sketch to interior/exterior/garden.
_GENERATE_MODES = {"photo", "text", "sketch"}


# ---------------------------------------------------------------------------
# Circuit breaker (B3) — auto-trip the kill switch on elevated vendor error
# rate, not just on budget. Counter lives in the site cache with a sliding
# expiry; content-level failures (safety, moderation) don't count because they
# say nothing about vendor health.
# ---------------------------------------------------------------------------

_CB_WINDOW_SEC = 600
_CB_THRESHOLD = 6
_CB_TRIP_HOURS = 0.5

_CB_COUNTED_CODES = {
    "VENDOR_NETWORK", "VENDOR_TIMEOUT", "VENDOR_RATE_LIMITED",
    "VENDOR_UNAVAILABLE", "VENDOR_HTTP", "VENDOR_RESPONSE", "WORKER_ERROR",
}


def _circuit_note_failure(tool_id, code):
    """Count a vendor failure and, past the threshold, take THIS tool offline.

    Two changes from the original. The counter is an atomic INCR — the
    get-then-set form undercounted exactly when failures were arriving fastest,
    which is when it mattered. And the trip is scoped to the failing tool
    rather than the global kill switch: six failures on one tool used to take
    the whole product offline, subscribers included, and their credits with it.
    """
    if code not in _CB_COUNTED_CODES:
        return
    try:
        cache = frappe.cache()
        rkey = cache.make_key(f"ai_cb2:{tool_id}")
        current = int(cache.incrby(rkey, 1))
        if current == 1:
            cache.expire(rkey, _CB_WINDOW_SEC)
        if current >= _CB_THRESHOLD:
            ai_cfg.trip_tool_breaker(
                tool_id,
                hours=_CB_TRIP_HOURS,
                reason=(
                    f"Circuit breaker: {current} vendor failures for "
                    f"{tool_id} within {_CB_WINDOW_SEC // 60} min "
                    f"(last: {code})"
                ),
            )
            cache.delete(rkey)
    except Exception:
        frappe.log_error(traceback.format_exc(), "ai_worker.circuit_breaker")


def _circuit_note_success(tool_id):
    try:
        cache = frappe.cache()
        cache.delete(cache.make_key(f"ai_cb2:{tool_id}"))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# HTTP helpers — retry-with-backoff on transient vendor failures (B1)
# ---------------------------------------------------------------------------

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}

_VENDOR_USER_MESSAGES = {
    "VENDOR_RATE_LIMITED": "The AI service is busy right now. Please try again in a minute.",
    "VENDOR_UNAVAILABLE": "The AI service is temporarily unavailable. Please try again shortly.",
    "VENDOR_TIMEOUT": "The AI service took too long to respond. Please try again.",
    "VENDOR_NETWORK": "Could not reach the AI service. Please check back shortly.",
    "VENDOR_HTTP": "The AI service could not process this request. Please try again.",
    "VENDOR_RESPONSE": "The AI returned an unexpected result. Please try again.",
}


def _http_error(status, vendor, body_snippet):
    if status == 429:
        code = "VENDOR_RATE_LIMITED"
    elif status >= 500:
        code = "VENDOR_UNAVAILABLE"
    else:
        code = "VENDOR_HTTP"
    return _VendorError(
        code,
        _VENDOR_USER_MESSAGES[code],
        detail=f"{vendor} returned {status}: {body_snippet}",
    )


def _post_json_with_retry(url, body, timeout, vendor, retries=2, headers=None,
                          retry_timeouts=True):
    """POST returning the 200 Response; retries 429/5xx with a short backoff,
    fails fast on non-retryable 4xx (B1).

    `retry_timeouts=False` for anything that BILLS PER CALL. A client-side
    timeout cannot distinguish a slow success from a failure: the vendor may
    well have finished the generation and charged for it. Retrying such a call
    three times against four variations is up to twelve billable generations
    for one credit — so for image generation a timeout is terminal, and the
    user is refunded through the normal failure path.
    """
    last_err = None
    for attempt in range(retries + 1):
        try:
            resp = requests.post(url, json=body, timeout=timeout, headers=headers or None)
        except requests.Timeout as e:
            last_err = _VendorError(
                "VENDOR_TIMEOUT", _VENDOR_USER_MESSAGES["VENDOR_TIMEOUT"],
                detail=f"{vendor} timeout: {e}",
            )
            # A CONNECT timeout never reached the vendor, so nothing was
            # generated and nothing was billed — it stays retryable even for
            # per-call billing. Only a read timeout is ambiguous (the vendor
            # may have finished and charged), and that is what
            # `retry_timeouts=False` is guarding against.
            if not retry_timeouts and not isinstance(e, requests.ConnectTimeout):
                raise last_err
        except requests.RequestException as e:
            last_err = _VendorError(
                "VENDOR_NETWORK", _VENDOR_USER_MESSAGES["VENDOR_NETWORK"],
                detail=f"{vendor} request failed: {e}",
            )
        else:
            if resp.status_code == 200:
                return resp
            last_err = _http_error(resp.status_code, vendor, resp.text[:300])
            if resp.status_code not in _RETRYABLE_STATUS:
                raise last_err
        if attempt < retries:
            time.sleep(1.5 * (attempt + 1))
    raise last_err


# ---------------------------------------------------------------------------
# Gemini safety handling (A3) — a safety refusal is a distinct, user-facing
# outcome, not a generic VENDOR_RESPONSE.
# ---------------------------------------------------------------------------

_SAFETY_FINISH_REASONS = {
    "SAFETY", "PROHIBITED_CONTENT", "IMAGE_SAFETY", "BLOCKLIST", "SPII",
}

_SAFETY_USER_MESSAGE = (
    "This photo or request couldn't be processed. "
    "Try a different photo or simpler notes."
)


def _check_gemini_safety(data):
    """Raise SAFETY_BLOCKED when Gemini refused the prompt or the response."""
    fb = (data or {}).get("promptFeedback") or {}
    if fb.get("blockReason"):
        raise _VendorError(
            "SAFETY_BLOCKED", _SAFETY_USER_MESSAGE,
            detail=f"Gemini promptFeedback.blockReason={fb.get('blockReason')}",
        )
    for cand in (data or {}).get("candidates") or []:
        reason = (cand.get("finishReason") or "").upper()
        if reason in _SAFETY_FINISH_REASONS:
            raise _VendorError(
                "SAFETY_BLOCKED", _SAFETY_USER_MESSAGE,
                detail=f"Gemini finishReason={reason}",
            )


# ---------------------------------------------------------------------------
# Free-text sanitization (A2) — notes/style/room/color are user input that is
# concatenated into the vendor prompt. Submit-side validation in v1 rejects
# blocklisted notes before any credit is debited; this is defense-in-depth for
# payloads that reach the worker via other paths (legacy endpoints, replays).
# ---------------------------------------------------------------------------

_NOTES_MAX_LEN = 300
_SLUG_MAX_LEN = 60

# Substring match, lowercased. Injection phrases + clearly-abusive content.
# Deliberately short: the vendor safety filter (A3) is the real backstop.
NOTES_BLOCKLIST = (
    "ignore previous", "ignore all previous", "ignore the above",
    "disregard previous", "disregard the above", "system prompt",
    "you are now", "new instructions", "jailbreak", "do anything now",
    "nsfw", "nude", "naked", "topless", "undress", "porn", "sexual",
    "erotic", "xxx", "gore", "beheading", "corpse", "mutilat",
)

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def notes_blocked_term(text):
    """Return the first blocklisted term found in text, else None."""
    low = (text or "").lower()
    for term in NOTES_BLOCKLIST:
        if term in low:
            return term
    return None


def _clean_prompt_field(value, max_len):
    """Strip control chars, collapse whitespace, hard-cap length."""
    if not value:
        return None
    text = _CONTROL_CHARS_RE.sub(" ", str(value))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_len] or None


# H2: expand client style slugs into rich prompt phrases. Pakistan-first
# entries name real, locally available materials so a render maps to things a
# local builder can actually buy — keep in sync with AiCatalog.styles in the
# Flutter app (lib/features/ai/data/ai_catalog.dart).
_STYLE_PHRASES = {
    # interior
    "modern": "modern (clean lines, neutral palette)",
    "minimal": "minimalist (whites, soft natural wood)",
    "industrial": "industrial (exposed concrete, matte black metal)",
    "scandi": "Scandinavian (light wood, soft textiles)",
    "classic": "classic (warm wood, traditional ornament)",
    "boho": "bohemian (layered textiles, plants)",
    "japandi": "Japandi (quiet, handmade, low furniture)",
    "heritage": (
        "traditional Pakistani heritage (carved sheesham wood furniture, "
        "jharoka-style arches, brass accents, handwoven rugs)"
    ),
    "lux": "luxury (marble surfaces, brass details)",
    # exterior
    "mediterranean": "Mediterranean (stucco walls, terracotta roof)",
    "colonial": "colonial (symmetric facade, columns)",
    "desi": (
        "Pakistani brick (warm gutka brick facade, jali screens, "
        "cantilever shades)"
    ),
    "gwalior": (
        "local stone facade (beige sandstone cladding, grey granite trim, "
        "minimal glazing)"
    ),
    "farmhouse": "farmhouse (pitched roof, porch, natural materials)",
    "contemp": "contemporary (mixed materials, bold massing)",
    # garden
    "tropical": "tropical (lush dense planting)",
    "desert": "desert (drought-friendly stone and succulents)",
    "english": "English cottage (perennial borders)",
    "zen": "zen (raked gravel, sculpted shrubs)",
    "charbagh": (
        "Mughal charbagh (symmetric quadrant lawns, central water channel, "
        "cypress and citrus planting)"
    ),
    "modernlh": "modern lawn (geometric beds, clean hardscape)",
    "edible": "edible garden (vegetable beds, fruit trees)",
}


_ROOM_PHRASES = {
    "living": "living room",
    "bedroom": "bedroom",
    "kitchen": "kitchen",
    "dining": "dining room",
    "bath": "bathroom",
    "office": "home office",
    "kids": "kids room",
    "lounge": "lounge",
}


def _sanitize_prompt_inputs(payload):
    """Sanitized {style, room, color, notes} for the prompt builders (A2),
    with style/room slugs expanded to readable prompt phrases (H2).

    Blocklisted notes are dropped entirely rather than partially scrubbed —
    a scrubbed injection attempt is still an injection attempt."""
    notes = _clean_prompt_field(payload.get("notes"), _NOTES_MAX_LEN)
    if notes and notes_blocked_term(notes):
        notes = None
    style = _clean_prompt_field(payload.get("style"), _SLUG_MAX_LEN)
    if style:
        style = _STYLE_PHRASES.get(style.lower(), style)
    room = _clean_prompt_field(payload.get("room"), _SLUG_MAX_LEN)
    if room:
        room = _ROOM_PHRASES.get(room.lower(), room)
    return {
        "style": style,
        "room": room,
        "color": _clean_prompt_field(payload.get("color"), _SLUG_MAX_LEN),
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# Input moderation (A1) — cheap Gemini vision gate on the source photo before
# the expensive generation call. Fails CLOSED on an explicit unsafe/mismatch
# verdict, fails OPEN on infrastructure hiccups (a moderation outage must not
# take the whole product down; the vendor's own safety filter still applies).
# ---------------------------------------------------------------------------

_MODERATION_MODEL = "gemini-2.0-flash"

_MODERATION_PROMPT = (
    "You are a strict content gate for a home-design app. Classify the image. "
    "Reply ONLY with this JSON: "
    '{"category": one of ["home_interior","home_exterior","garden_outdoor",'
    '"floor_plan","other"], "safe": true|false}. '
    'Set "safe" to false for nudity or sexual content, graphic violence, '
    "or imagery of identifiable people as the main subject. "
    'Rooms, buildings, gardens and architectural drawings are "safe": true.'
)

_MODERATION_ALLOWED = {
    "floor_plan": {"floor_plan"},
    # Every image-edit tool accepts any home-ish scene.
    "_image_edit": {"home_interior", "home_exterior", "garden_outdoor"},
}

_MODERATION_REJECT_MESSAGE = (
    "This doesn't look like a photo of a room, building, garden or floor "
    "plan. Please upload a photo of your space."
)

_MODERATION_UNSAFE_MESSAGE = (
    "This photo can't be processed. Please upload a photo of a room, "
    "building, garden or floor plan."
)


def _moderate_source_image(tool_id, image_bytes, mime, allow_any_category=False):
    """Gate the uploaded photo (A1). Returns the moderation token usage dict
    (or None when skipped) so callers can fold it into the job's totals.

    `allow_any_category` (C2 sketch mode): hand-drawn sketches classify
    unpredictably (floor_plan/other), so only the safe flag is enforced."""
    if not image_bytes:
        return None
    if frappe.conf.get("ai_moderation_disabled"):
        return None
    if not ai_cfg.get_api_key("gemini"):
        # Can't moderate without a key; the generation vendor's own safety
        # filter (A3) remains the backstop.
        return None

    try:
        text, usage = _gemini_vision(
            _MODERATION_MODEL, _MODERATION_PROMPT, image_bytes, mime
        )
        verdict = json.loads(text)
    except _VendorError as e:
        if e.code == "SAFETY_BLOCKED":
            # Gemini refused to even look at it — definitely reject.
            raise _VendorError("MODERATION_REJECTED", _MODERATION_UNSAFE_MESSAGE,
                               detail="moderation call safety-blocked")
        frappe.log_error(
            f"moderation skipped (fail-open): {e.code}: {e.detail or e}",
            "ai_worker.moderation",
        )
        return None
    except (json.JSONDecodeError, TypeError):
        frappe.log_error(
            f"moderation verdict unparseable (fail-open): {text[:200]}",
            "ai_worker.moderation",
        )
        return usage

    if verdict.get("safe") is False:
        raise _VendorError(
            "MODERATION_REJECTED", _MODERATION_UNSAFE_MESSAGE,
            detail=f"moderation verdict: {verdict}",
        )
    if allow_any_category:
        return usage
    allowed = _MODERATION_ALLOWED.get(tool_id) or _MODERATION_ALLOWED["_image_edit"]
    category = (verdict.get("category") or "").lower()
    if category and category not in allowed:
        raise _VendorError(
            "MODERATION_REJECTED", _MODERATION_REJECT_MESSAGE,
            detail=f"moderation verdict: {verdict} not in {sorted(allowed)}",
        )
    return usage


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
            raise _VendorError(
                "FILE_MISSING",
                "We couldn't read your uploaded photo. Please upload it again.",
                detail=f"Could not load uploaded file {image_url}",
            )
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


def _wm_font(size):
    """Best-available font for the watermark. DejaVu ships with Pillow; fall
    back to the bitmap default if it's somehow missing."""
    from PIL import ImageFont
    for name in ("DejaVuSans-Bold.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _watermark_bytes(content, label="BuildCost Pro"):
    """Burn a tiled diagonal mark + a solid corner badge into image bytes for
    free-tier output. Returns (jpeg_bytes, "image/jpeg"), or (content, None) on
    any failure so a watermarking hiccup never fails the whole job."""
    try:
        from PIL import Image, ImageDraw

        base = Image.open(io.BytesIO(content)).convert("RGBA")
        w, h = base.size

        # Tiled, semi-transparent diagonal text across the whole image — hard to
        # crop out cleanly.
        layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        fsize = max(18, w // 28)
        font = _wm_font(fsize)
        bb = draw.textbbox((0, 0), label, font=font)
        tw, th = bb[2] - bb[0], bb[3] - bb[1]
        step_x, step_y = tw + fsize * 3, th + fsize * 3
        row = 0
        y = -h
        while y < h * 2:
            offset = 0 if row % 2 == 0 else step_x // 2
            x = -w + offset
            while x < w * 2:
                draw.text((x, y), label, font=font, fill=(255, 255, 255, 64))
                x += step_x
            y += step_y
            row += 1
        layer = layer.rotate(30, expand=False)
        base = Image.alpha_composite(base, layer)

        # Solid corner badge, bottom-right, on a translucent bar.
        d2 = ImageDraw.Draw(base, "RGBA")
        bfsize = max(20, w // 22)
        bfont = _wm_font(bfsize)
        cb = d2.textbbox((0, 0), label, font=bfont)
        bw, bh = cb[2] - cb[0], cb[3] - cb[1]
        pad = max(6, bfsize // 2)
        x2, y2 = w - pad, h - pad
        x1, y1 = x2 - bw - pad * 2, y2 - bh - pad * 2
        d2.rounded_rectangle([x1, y1, x2, y2], radius=pad, fill=(0, 0, 0, 140))
        d2.text((x1 + pad - cb[0], y1 + pad - cb[1]), label, font=bfont,
                fill=(255, 255, 255, 235))

        out = io.BytesIO()
        base.convert("RGB").save(out, format="JPEG", quality=90)
        return out.getvalue(), "image/jpeg"
    except Exception:
        frappe.log_error(traceback.format_exc(), "ai_worker.watermark_failed")
        return content, None


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

    if image_bytes is None:
        # Submit validates the image is present; this guards legacy/replayed
        # payloads from a b64encode(None) crash that would count toward the
        # circuit breaker as WORKER_ERROR.
        raise _VendorError(
            "FILE_MISSING",
            "We couldn't read your uploaded photo. Please upload it again.",
            detail=f"job {doc.name}: floor_plan with no readable image_url",
        )

    mod_usage = _moderate_source_image("floor_plan", image_bytes, mime) or {}

    if vendor == "gemini":
        text, usage = _gemini_vision(model, _FLOOR_PLAN_PROMPT, image_bytes, mime)
        doc.model = model
        doc.tokens_input = (
            usage.get("input_tokens", 0) + mod_usage.get("input_tokens", 0)
        )
        doc.tokens_output = (
            usage.get("output_tokens", 0) + mod_usage.get("output_tokens", 0)
        )
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
    resp = _post_json_with_retry(url, body, timeout=60, vendor="Gemini")
    data = resp.json()
    _check_gemini_safety(data)
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        raise _VendorError(
            "VENDOR_RESPONSE", _VENDOR_USER_MESSAGES["VENDOR_RESPONSE"],
            detail=f"Unexpected Gemini shape: {str(data)[:500]}",
        )
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
            raise _VendorError(
                "VENDOR_RESPONSE", _VENDOR_USER_MESSAGES["VENDOR_RESPONSE"],
                detail="Floor-plan response was not JSON.",
            )
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


# C2: text mode — create from imagination, no source photo. The photo-mode
# builders all say "this room/facade" and "preserve the original geometry",
# which is meaningless (and confusing to the model) without an input image.
def _prompt_text_mode(tool_id, p):
    if tool_id == "interior":
        base = (
            f"Photorealistic interior design concept of a "
            f"{p.get('room') or 'living room'} in {p.get('style') or 'modern'} style"
        )
    elif tool_id == "exterior":
        base = (
            "Photorealistic architectural rendering of a residential house "
            f"exterior in {p.get('style') or 'modern'} style"
        )
    else:
        base = (
            "Photorealistic landscape design concept of a home garden "
            f"in {p.get('style') or 'tropical'} style"
        )
    parts = [base]
    if p.get("color"):
        parts.append(f"with a {p['color']} color palette")
    if p.get("notes"):
        parts.append(p["notes"])
    parts.append(
        "Create the scene from imagination — there is no source photo. "
        "Wide-angle view, realistic materials, natural lighting, magazine quality."
    )
    return ", ".join(parts)


# C2: sketch mode — the uploaded image is a rough hand drawing, not a photo.
def _prompt_sketch_mode(tool_id, p):
    subject = {
        "interior": f"{p.get('room') or 'room'} interior",
        "exterior": "residential building exterior",
        "garden": "garden or outdoor area",
    }.get(tool_id, "space")
    parts = [
        f"The attached image is a rough hand-drawn sketch or plan of a {subject}",
        f"render it as a photorealistic {p.get('style') or 'modern'} style visualization",
    ]
    if p.get("color"):
        parts.append(f"with a {p['color']} color palette")
    if p.get("notes"):
        parts.append(p["notes"])
    parts.append(
        "Follow the sketch's layout, proportions and viewpoint as closely as "
        "possible. Realistic materials and natural lighting."
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

    # C2: text mode runs with no source image at all; sketch mode uploads a
    # hand drawing instead of a photo. Everything else requires the photo.
    mode = (payload.get("mode") or "photo").strip().lower()
    if mode not in _GENERATE_MODES:
        mode = "photo"

    image_bytes = mime = None
    if mode != "text":
        image_bytes, mime = _read_uploaded_file(payload.get("image_url"))
        if image_bytes is None:
            # Submit guarantees an image outside text mode; this guards
            # legacy/replayed payloads from a b64encode(None) crash.
            raise _VendorError(
                "FILE_MISSING",
                "We couldn't read your uploaded photo. Please upload it again.",
                detail=f"job {doc.name}: mode={mode} with no readable image_url",
            )
    mask_bytes = mask_mime = None
    ref_bytes = ref_mime = None
    if payload.get("mask_url"):
        mask_bytes, mask_mime = _read_uploaded_file(payload["mask_url"])
    if payload.get("ref_image_url"):
        ref_bytes, ref_mime = _read_uploaded_file(payload["ref_image_url"])

    builder = _PROMPT_BUILDERS.get(tool_id)
    if not builder:
        raise _VendorError(
            "UNKNOWN_TOOL", "This tool is not available.",
            detail=f"No prompt builder for tool {tool_id}",
        )
    p = _sanitize_prompt_inputs(payload)
    if mode == "text":
        prompt = _prompt_text_mode(tool_id, p)
    elif mode == "sketch":
        prompt = _prompt_sketch_mode(tool_id, p)
    else:
        prompt = builder(p)

    variations = int(doc.variation_count or 1)
    hd = (doc.quality or "std").lower() == "hd"

    if vendor == "mock":
        doc.model = "mock-image-edit"
        doc.cost_cents = 0
        return _mock_image_edit(tool_id, payload, variations, prompt, hd)

    # Sketches classify as floor_plan/other, so only enforce the safe flag on
    # them; text mode has no image and the gate no-ops.
    mod_usage = _moderate_source_image(
        tool_id, image_bytes, mime, allow_any_category=(mode == "sketch")
    ) or {}

    if vendor == "gemini":
        result = _gemini_image_edit(
            doc, model, prompt, image_bytes, mime,
            mask_bytes, mask_mime, ref_bytes, ref_mime, variations, hd
        )
    elif vendor == "fal":
        result = _fal_image_edit(
            doc, model, prompt, image_bytes, mime,
            mask_bytes, ref_bytes, variations, hd
        )
    else:
        raise _AIDisabled(f"Image-edit vendor {vendor} is not implemented yet.")

    # Fold the moderation pass's token spend into the job's accounting.
    doc.tokens_input = (doc.tokens_input or 0) + mod_usage.get("input_tokens", 0)
    doc.tokens_output = (doc.tokens_output or 0) + mod_usage.get("output_tokens", 0)
    return result


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
    # C2 text mode is pure text-to-image — no source image part.
    if image_bytes:
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
        # retry_timeouts=False: each call is a billable generation.
        resp = _post_json_with_retry(
            url, body, timeout=120, vendor="Gemini", retry_timeouts=False
        )
        data = resp.json()
        _check_gemini_safety(data)
        return data

    # Collect per-future rather than with `ex.map`, which re-raises the first
    # exception it meets and throws away every sibling result — so one late
    # failure used to discard three images the operator had already paid for.
    results = []
    first_error = None
    with ThreadPoolExecutor(max_workers=min(4, variations)) as ex:
        futures = [ex.submit(_call_once, i) for i in range(variations)]
        for fut in futures:
            try:
                results.append(fut.result())
            except Exception as e:  # noqa: BLE001 - re-raised below if total
                if first_error is None:
                    first_error = e

    if not results:
        # Nothing survived — this is an ordinary job failure and the standard
        # refund path applies.
        raise first_error
    if first_error is not None:
        frappe.log_error(
            f"job {doc.name}: {len(results)}/{variations} variations succeeded; "
            f"first error: {first_error}",
            "ai_worker.partial_variations",
        )

    images = []
    total_tokens_in = 0
    total_tokens_out = 0
    # Free-tier output is watermarked server-side so the raw, clean image is
    # never reachable — even by the owner reading the private file URL. Premium
    # output is delivered clean.
    # F-42: the entitlement the job was PRICED with, frozen at submit. Re-deriving
    # it here meant a subscription lapsing while the job sat in the queue burned a
    # watermark into an image the user had paid full Pro price for.
    watermark = not int(getattr(doc, "charged_as_premium", 0) or 0)
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
                raise _VendorError(
                    "VENDOR_RESPONSE", _VENDOR_USER_MESSAGES["VENDOR_RESPONSE"],
                    detail=f"Gemini response had no image part: {str(data)[:500]}",
                )
            img_bytes = base64.b64decode(inline.get("data") or inline.get("data_b64") or "")
            img_mime = inline.get("mime_type") or inline.get("mimeType") or "image/png"
            if watermark:
                wm_bytes, wm_mime = _watermark_bytes(img_bytes)
                if wm_mime:
                    img_bytes, img_mime = wm_bytes, wm_mime
            url_saved = _save_result_image(f"ai_{doc.name}_v{i}", img_bytes, img_mime)
            images.append({"url": url_saved, "thumb_url": url_saved, "seed": 1000 + i * 17})
        except (KeyError, IndexError):
            raise _VendorError(
                "VENDOR_RESPONSE", _VENDOR_USER_MESSAGES["VENDOR_RESPONSE"],
                detail=f"Unexpected Gemini shape: {str(data)[:500]}",
            )
        usage = data.get("usageMetadata") or {}
        total_tokens_in += int(usage.get("promptTokenCount", 0))
        total_tokens_out += int(usage.get("candidatesTokenCount", 0))

    # The user was charged for `variations`. If fewer came back, give the
    # difference back rather than quietly keeping it.
    if len(images) < variations:
        try:
            from construction.api.v1 import _refund_partial_credits
            _refund_partial_credits(
                doc.user, "AI Job", doc.name,
                delivered=len(images), paid_for=variations,
                job_idempotency_key=doc.idempotency_key,
            )
        except Exception:
            frappe.log_error(
                traceback.format_exc(), f"ai_worker.partial_refund:{doc.name}"
            )

    doc.model = model
    doc.tokens_input = total_tokens_in
    doc.tokens_output = total_tokens_out
    # Gemini 2.5 Flash Image lists at ~$0.039/image. Add 50% for HD as a buffer
    # (Google may bill more for larger outputs).
    per_image_cents = int(round(3.9 * (1.5 if hd else 1.0)))
    doc.cost_cents = max(1, per_image_cents * len(images))

    return {"images": images, "prompt": prompt, "model": model, "hd": hd}


# ---------------------------------------------------------------------------
# fal.ai adapter (B2) — FLUX Kontext image-to-image via the fal queue API.
# Same contract as _gemini_image_edit. Selected per-tool by setting the
# AI Tool Config row's vendor to "fal"; model must be a fal model id
# (e.g. "fal-ai/flux-kontext/dev"), otherwise the default below is used.
# ---------------------------------------------------------------------------

_FAL_DEFAULT_MODEL = "fal-ai/flux-kontext/dev"
_FAL_QUEUE_BASE = "https://queue.fal.run"
_FAL_POLL_INTERVAL_SEC = 2
_FAL_POLL_TIMEOUT_SEC = 240


def _fal_image_edit(doc, model, prompt, image_bytes, mime,
                    mask_bytes, ref_bytes, variations, hd):
    api_key = _require_key("fal")
    # A Gemini model string on the config row (leftover from a vendor flip)
    # is not a fal id — fal ids always contain a slash.
    model_id = model if "/" in (model or "") else _FAL_DEFAULT_MODEL

    if mask_bytes or ref_bytes:
        # FLUX Kontext (dev) takes a single input image; silently ignoring the
        # mask/reference would produce results the user didn't ask for.
        raise _VendorError(
            "VENDOR_UNSUPPORTED",
            "This tool isn't available right now. Please try another tool.",
            detail=f"fal adapter has no mask/ref support (model {model_id})",
        )
    if image_bytes is None:
        # C2 text mode: Kontext is image-to-image only.
        raise _VendorError(
            "VENDOR_UNSUPPORTED",
            "Creating a design without a photo isn't available right now. "
            "Please start from a photo instead.",
            detail=f"fal adapter requires a source image (model {model_id})",
        )

    headers = {"Authorization": f"Key {api_key}"}
    data_uri = (
        f"data:{mime or 'image/jpeg'};base64,"
        + base64.b64encode(image_bytes).decode("ascii")
    )
    body = {
        "prompt": prompt,
        "image_url": data_uri,
        "num_images": max(1, min(4, variations)),
        "output_format": "jpeg",
    }

    submit = _post_json_with_retry(
        f"{_FAL_QUEUE_BASE}/{model_id}", body,
        timeout=60, vendor="fal.ai", headers=headers,
    ).json()
    request_id = submit.get("request_id")
    if not request_id:
        raise _VendorError(
            "VENDOR_RESPONSE", _VENDOR_USER_MESSAGES["VENDOR_RESPONSE"],
            detail=f"fal submit had no request_id: {str(submit)[:300]}",
        )

    status_url = f"{_FAL_QUEUE_BASE}/{model_id}/requests/{request_id}/status"
    result_url = f"{_FAL_QUEUE_BASE}/{model_id}/requests/{request_id}"

    deadline = time.monotonic() + _FAL_POLL_TIMEOUT_SEC
    while True:
        if time.monotonic() > deadline:
            raise _VendorError(
                "VENDOR_TIMEOUT", _VENDOR_USER_MESSAGES["VENDOR_TIMEOUT"],
                detail=f"fal request {request_id} still queued after "
                       f"{_FAL_POLL_TIMEOUT_SEC}s",
            )
        try:
            status_resp = requests.get(status_url, headers=headers, timeout=30)
        except requests.RequestException:
            time.sleep(_FAL_POLL_INTERVAL_SEC)
            continue
        if status_resp.status_code != 200:
            time.sleep(_FAL_POLL_INTERVAL_SEC)
            continue
        status = (status_resp.json().get("status") or "").upper()
        if status == "COMPLETED":
            break
        if status in {"FAILED", "ERROR", "CANCELLED"}:
            raise _VendorError(
                "VENDOR_UNAVAILABLE", _VENDOR_USER_MESSAGES["VENDOR_UNAVAILABLE"],
                detail=f"fal request {request_id} ended {status}",
            )
        time.sleep(_FAL_POLL_INTERVAL_SEC)

    try:
        result = requests.get(result_url, headers=headers, timeout=60).json()
    except (requests.RequestException, ValueError) as e:
        raise _VendorError(
            "VENDOR_RESPONSE", _VENDOR_USER_MESSAGES["VENDOR_RESPONSE"],
            detail=f"fal result fetch failed: {e}",
        )

    remote_images = result.get("images") or []
    if not remote_images:
        # fal surfaces NSFW filtering as an empty/flagged image list.
        if result.get("has_nsfw_concepts"):
            raise _VendorError("SAFETY_BLOCKED", _SAFETY_USER_MESSAGE,
                               detail=f"fal flagged NSFW: {str(result)[:300]}")
        raise _VendorError(
            "VENDOR_RESPONSE", _VENDOR_USER_MESSAGES["VENDOR_RESPONSE"],
            detail=f"fal result had no images: {str(result)[:300]}",
        )

    # F-42: the entitlement the job was PRICED with, frozen at submit. Re-deriving
    # it here meant a subscription lapsing while the job sat in the queue burned a
    # watermark into an image the user had paid full Pro price for.
    watermark = not int(getattr(doc, "charged_as_premium", 0) or 0)
    images = []
    for i, entry in enumerate(remote_images):
        img_url = entry.get("url")
        if not img_url:
            continue
        try:
            img_resp = requests.get(img_url, timeout=60)
            img_resp.raise_for_status()
        except requests.RequestException as e:
            raise _VendorError(
                "VENDOR_NETWORK", _VENDOR_USER_MESSAGES["VENDOR_NETWORK"],
                detail=f"fal image download failed: {e}",
            )
        img_bytes = img_resp.content
        img_mime = entry.get("content_type") or "image/jpeg"
        if watermark:
            wm_bytes, wm_mime = _watermark_bytes(img_bytes)
            if wm_mime:
                img_bytes, img_mime = wm_bytes, wm_mime
        url_saved = _save_result_image(f"ai_{doc.name}_v{i}", img_bytes, img_mime)
        images.append({
            "url": url_saved,
            "thumb_url": url_saved,
            "seed": result.get("seed") or (1000 + i * 17),
        })

    if not images:
        raise _VendorError(
            "VENDOR_RESPONSE", _VENDOR_USER_MESSAGES["VENDOR_RESPONSE"],
            detail="fal result images all lacked URLs",
        )

    doc.model = model_id
    doc.tokens_input = 0
    doc.tokens_output = 0
    # FLUX Kontext dev lists at ~$0.025/image; keep a small buffer.
    per_image_cents = 4 if hd else 3
    doc.cost_cents = max(1, per_image_cents * len(images))

    return {"images": images, "prompt": prompt, "model": model_id, "hd": hd}


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
