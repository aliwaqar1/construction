# B5: golden-pair regression checks for model/prompt drift.
#
# Vendor model strings ("gemini-2.5-flash-image") are mutable aliases: Google
# can revise the model behind the name and output quality silently shifts.
# This runs a small FIXED set of (synthetic photo, style) pairs through the
# live vendor path and records the outcome + result file URLs, so a human can
# eyeball drift pre-release and an outright breakage (no image back) is
# caught automatically.
#
# Run manually (spends a few cents of real vendor budget):
#   bench --site construction.local execute construction.api.ai_golden.run_golden_checks
#
# Or weekly via the scheduler by setting `"ai_golden_weekly": 1` in
# site_config.json — deliberately opt-in, because it spends real money.

import io
import json
import time
import traceback

import frappe

from construction.construction.doctype.ai_settings import ai_settings as ai_cfg


# The fixed golden set. Deterministic synthetic scenes (drawn in code, below)
# so the input can never rot or leak someone's real home photo into logs.
GOLDEN_PAIRS = [
    ("interior", "modern"),
    ("exterior", "desi"),
    ("floor", "lux"),
]


def _golden_image():
    """A deterministic, room-ish synthetic photo (512x384 JPEG)."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (512, 384), (222, 215, 200))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 260, 512, 384], fill=(150, 130, 110))    # floor
    d.rectangle([60, 140, 220, 260], fill=(90, 100, 120))    # sofa block
    d.rectangle([320, 60, 440, 180], fill=(200, 220, 240))   # window
    d.rectangle([325, 65, 435, 175], outline=(120, 120, 120), width=3)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return buf.getvalue(), "image/jpeg"


class _GoldenDoc:
    """Minimal stand-in for an AI Job doc — the vendor adapters only set
    accounting fields on it and read .name/.user/.variation_count/.quality."""

    def __init__(self, tag):
        self.name = f"golden_{tag}_{int(time.time())}"
        self.user = "Administrator"  # premium path → clean, unwatermarked output
        self.variation_count = 1
        self.quality = "std"
        self.model = None
        self.tokens_input = 0
        self.tokens_output = 0
        self.cost_cents = 0


def run_golden_checks(pairs=None):
    """Run the golden set against the LIVE vendor config. Returns a summary
    list; logs one Error Log entry ("ai_golden.summary") with the full JSON,
    and a loud one per hard failure."""
    from construction.api import ai_worker

    results = []
    image_bytes, mime = _golden_image()

    for tool_id, style in (pairs or GOLDEN_PAIRS):
        cfg = ai_cfg.get_job_config(tool_id)
        vendor = (cfg.get("vendor") or "mock").lower()
        entry = {
            "tool": tool_id,
            "style": style,
            "vendor": vendor,
            "model": cfg.get("model"),
            "ok": None,
        }
        if vendor == "mock":
            entry["ok"] = None
            entry["note"] = "vendor is mock — nothing to regression-test"
            results.append(entry)
            continue

        doc = _GoldenDoc(f"{tool_id}_{style}")
        prompt = ai_worker._PROMPT_BUILDERS[tool_id](
            ai_worker._sanitize_prompt_inputs({"style": style, "room": "living"})
        )
        started = time.monotonic()
        try:
            if vendor == "gemini":
                result = ai_worker._gemini_image_edit(
                    doc, cfg.get("model") or "gemini-2.5-flash-image", prompt,
                    image_bytes, mime, None, None, None, None, 1, False,
                )
            elif vendor == "fal":
                result = ai_worker._fal_image_edit(
                    doc, cfg.get("model"), prompt, image_bytes, mime,
                    None, None, 1, False,
                )
            else:
                entry["ok"] = None
                entry["note"] = f"no golden adapter for vendor {vendor}"
                results.append(entry)
                continue
            entry["ok"] = bool(result.get("images"))
            entry["latency_s"] = round(time.monotonic() - started, 1)
            entry["result_urls"] = [i.get("url") for i in result.get("images", [])]
            entry["cost_cents"] = doc.cost_cents
        except Exception as e:
            entry["ok"] = False
            entry["latency_s"] = round(time.monotonic() - started, 1)
            entry["error"] = f"{type(e).__name__}: {e}"
            frappe.log_error(
                f"Golden check FAILED for {tool_id}/{style} on {vendor}:\n"
                f"{traceback.format_exc()}",
                "ai_golden.failure",
            )
        results.append(entry)

    frappe.log_error(json.dumps(results, indent=2), "ai_golden.summary")
    try:
        _prune_old_golden_files()
    except Exception:
        frappe.log_error(frappe.get_traceback(), "ai_golden.prune_failed")
    return results


# Keep a few runs of history for pre-release drift eyeballing, then delete.
_GOLDEN_RETENTION_DAYS = 45


def _prune_old_golden_files():
    """Golden runs save real File rows (ai_golden_*_v0.png via
    _save_result_image) that no doctype references and nothing else deletes —
    weekly runs would accumulate them forever (L2)."""
    from frappe.utils import add_days, now_datetime

    cutoff = add_days(now_datetime(), -_GOLDEN_RETENTION_DAYS)
    old = frappe.get_all(
        "File",
        filters={
            "file_name": ["like", "ai_golden_%"],
            "creation": ["<", cutoff],
        },
        pluck="name",
    )
    for name in old:
        frappe.delete_doc(
            "File", name, ignore_permissions=True, force=True,
            delete_permanently=True,
        )
    if old:
        frappe.db.commit()


def run_weekly():
    """Scheduler entry — opt-in via site_config `ai_golden_weekly: 1` because
    every run spends real vendor budget."""
    if not frappe.conf.get("ai_golden_weekly"):
        return
    run_golden_checks()
