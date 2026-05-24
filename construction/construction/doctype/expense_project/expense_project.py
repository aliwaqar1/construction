# Copyright (c) 2026, ali waqar and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class ExpenseProject(Document):
    """Server-side row for an expense tracker project. Owned by `user`;
    keyed by the device-generated `client_id` so push/pull is idempotent.

    Soft-deletes (`deleted=1`) are kept so sync can propagate deletions to
    other devices on the same account. Last-write-wins on `updated_at`.
    """
    pass
