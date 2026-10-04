# Copyright (c) 2026, ali waqar and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

_MAX_GRANT = 1000


class DebugCreditUser(Document):
    """Test accounts that a System Manager can top up with credits from the
    "Grant Credits" button on this form. Grants land in the AI Credit Ledger
    as `adjustment` rows in the topup bucket, so they never count as sales.
    """
    pass


@frappe.whitelist(methods=["POST"])
def grant_credits(name, amount):
    frappe.only_for("System Manager")
    amount = int(amount)
    if not 0 < amount <= _MAX_GRANT:
        frappe.throw(f"Amount must be between 1 and {_MAX_GRANT}.")
    user = frappe.get_doc("Debug Credit User", name).user

    from construction.api import v1

    try:
        with v1._user_ledger_lock(user):
            balance_after = v1._insert_ledger_event(
                user,
                event_type="adjustment",
                bucket="topup",
                delta=amount,
                reason=f"Debug grant by {frappe.session.user}",
                ref_doctype="Debug Credit User",
                ref_name=name,
            )
    except v1.LedgerLockError:
        frappe.throw("The ledger is busy for this user. Try again in a moment.")
    return balance_after
