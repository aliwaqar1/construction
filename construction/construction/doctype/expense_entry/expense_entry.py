# Copyright (c) 2026, ali waqar and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class ExpenseEntry(Document):
    """Server-side row for a single expense line. Owned by `user`; pairs
    to a parent `Expense Project` via `project_client_id`.

    Soft-deletes (`deleted=1`) are kept so sync can propagate deletions
    across devices. Last-write-wins on `updated_at`.
    """
    pass
