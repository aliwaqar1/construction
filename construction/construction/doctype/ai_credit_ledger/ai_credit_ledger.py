# Copyright (c) 2026, ali waqar and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class AICreditLedger(Document):
    """Append-only credit movement log.

    Every grant / debit / refund / clawback inserts a row. Balance is a
    projection (sum of deltas) computed by `v1._get_credit_state()`.
    The unique `idempotency_key` stops webhook retries from double-applying.

    Buckets:
      - monthly: per-period grants, resets each billing month (no rollover)
      - topup:   consumable packs, never expire
    Debits prefer monthly first, then topup.
    """
    pass
