import hashlib
import re

import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime

# How far back to look for an open issue with the same fingerprint before we
# aggregate into it instead of creating a new row.
_DEDUPE_WINDOW_DAYS = 14

# Field length guards so a runaway client can't bloat the table.
_LIMITS = {
    "title": 200,
    "message": 2000,
    "error_type": 140,
    "stack_trace": 12000,
    "endpoint": 500,
    "http_method": 10,
    "request_id": 64,
    "route": 200,
    "correlation_id": 64,
    "network_status": 40,
    "breadcrumbs": 12000,
    "context_json": 12000,
    "device_model": 140,
    "os_version": 60,
    "device_id": 100,
    "app_version": 50,
    "build_number": 30,
    "platform": 20,
    "environment": 20,
}


class AppErrorLog(Document):
    @staticmethod
    def _trim(value, field):
        if value is None:
            return None
        return str(value)[: _LIMITS.get(field, 1000)]

    @staticmethod
    def _normalise_for_fingerprint(text):
        """Strip volatile bits (numbers, hex ids, uuids) so the same logical
        error groups together regardless of ids/line offsets."""
        t = (text or "").lower()
        t = re.sub(r"0x[0-9a-f]+", "0xX", t)
        t = re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "<uuid>", t)
        t = re.sub(r"\d+", "N", t)
        return t.strip()

    @classmethod
    def _fingerprint(cls, data):
        if data.get("fingerprint"):
            return str(data["fingerprint"])[:64]
        basis = "|".join([
            str(data.get("source") or ""),
            str(data.get("error_type") or ""),
            cls._normalise_for_fingerprint(data.get("message")),
            str(data.get("route") or data.get("endpoint") or ""),
        ])
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]

    @classmethod
    def record(cls, **data):
        """Create or aggregate an App Error Log entry. Returns the row name.

        Idempotent-ish: an open issue (status New/Investigating) with the same
        fingerprint inside the dedupe window is incremented rather than
        duplicated. Never raises to the caller.
        """
        now = now_datetime()
        correlation_id = (
            cls._trim(data.get("correlation_id"), "correlation_id")
            or frappe.generate_hash(length=12)
        )
        fingerprint = cls._fingerprint(data)

        existing = frappe.db.get_value(
            "App Error Log",
            {
                "fingerprint": fingerprint,
                "status": ["in", ("New", "Investigating")],
                "creation": [">", frappe.utils.add_days(now, -_DEDUPE_WINDOW_DAYS)],
            },
            "name",
            order_by="creation desc",
        )

        if existing:
            doc = frappe.get_doc("App Error Log", existing)
            doc.occurrences = (doc.occurrences or 1) + 1
            doc.last_seen = now
            # Refresh the latest seen detail — most recent trace is most useful.
            if data.get("stack_trace"):
                doc.stack_trace = cls._trim(data.get("stack_trace"), "stack_trace")
            if data.get("app_version"):
                doc.app_version = cls._trim(data.get("app_version"), "app_version")
            doc.save(ignore_permissions=True)
            return doc.name

        title = cls._trim(data.get("title"), "title") or (
            f"{(data.get('source') or 'app')} · "
            f"{(data.get('message') or 'Unknown error')[:80]}"
        )

        doc = frappe.new_doc("App Error Log")
        doc.update({
            "title": title,
            "source": data.get("source") or "app",
            "level": data.get("level") or ("fatal" if data.get("is_fatal") else "error"),
            "is_fatal": 1 if data.get("is_fatal") else 0,
            "status": "New",
            "message": cls._trim(data.get("message"), "message"),
            "error_type": cls._trim(data.get("error_type"), "error_type"),
            "stack_trace": cls._trim(data.get("stack_trace"), "stack_trace"),
            "endpoint": cls._trim(data.get("endpoint"), "endpoint"),
            "http_method": cls._trim(data.get("http_method"), "http_method"),
            "http_status": frappe.utils.cint(data.get("http_status")) or None,
            "request_id": cls._trim(data.get("request_id"), "request_id"),
            "route": cls._trim(data.get("route"), "route"),
            "correlation_id": correlation_id,
            "network_status": cls._trim(data.get("network_status"), "network_status"),
            "breadcrumbs": cls._trim(data.get("breadcrumbs"), "breadcrumbs"),
            "context_json": cls._trim(data.get("context_json"), "context_json"),
            "device_model": cls._trim(data.get("device_model"), "device_model"),
            "os_version": cls._trim(data.get("os_version"), "os_version"),
            "device_id": cls._trim(data.get("device_id"), "device_id"),
            "app_version": cls._trim(data.get("app_version"), "app_version"),
            "build_number": cls._trim(data.get("build_number"), "build_number"),
            "platform": cls._trim(data.get("platform"), "platform"),
            "environment": data.get("environment") or "unknown",
            "user": data.get("user") if data.get("user") and data.get("user") != "Guest" else None,
            "fingerprint": fingerprint,
            "occurrences": 1,
            "first_seen": now,
            "last_seen": now,
        })
        doc.insert(ignore_permissions=True)
        return doc.name
