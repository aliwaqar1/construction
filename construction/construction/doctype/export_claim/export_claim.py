# Copyright (c) 2026, ali waqar and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class ExportClaim(Document):
    """One row = one account has spent its free export for one scope.

    The free PDF/CSV allowance used to live in the app's SharedPreferences,
    which made it a suggestion: clearing app data handed the user another one,
    forever. It is an entitlement, so it belongs where entitlements live.

    Scopes are independent — an estimate PDF and an expense report each get
    their own free export, matching what the app has always told users.

    `claim_key` is sha256(user|scope) with a unique index, so two exports
    racing each other resolve at the database rather than in application code:
    the loser gets a duplicate-key error, which is the correct answer.

    Pro users never reach this table; they are unlimited and nothing is
    recorded for them.
    """
    pass
