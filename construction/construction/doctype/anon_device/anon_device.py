# Copyright (c) 2026, Codestech and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class AnonDevice(Document):
	"""Binds one anonymous account to the device that created it.

	`anon_bootstrap` used to hand out an account's API credentials to anyone
	who named its `device_id` — and `device_id` is a client-chosen string the
	app publishes into URL query strings, an unauthenticated log table and
	Google Play Console. This row is what makes the device id useless on its
	own: re-fetching an account's keys requires the matching `device_secret`,
	which never leaves the device that generated it.
	"""

	pass
