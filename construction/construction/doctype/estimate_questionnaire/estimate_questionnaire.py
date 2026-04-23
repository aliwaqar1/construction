# Copyright (c) 2026, ali waqar and contributors
# For license information, please see license.txt

import json

import frappe
from frappe.model.document import Document


class EstimateQuestionnaire(Document):
    def validate(self):
        if self.steps_json:
            try:
                json.loads(self.steps_json)
            except json.JSONDecodeError:
                frappe.throw("Steps JSON is not valid JSON")
