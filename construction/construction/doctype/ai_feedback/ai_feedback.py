# Copyright (c) 2026, BuildCost Pro and contributors
# For license information, please see license.txt
#
# AI Feedback — one row per 👍/👎 a user leaves on a generated result (C3).
# Written via construction.api.v1.ai_feedback; read for prompt-quality
# analytics (failure themes per tool/style/model).

from frappe.model.document import Document


class AIFeedback(Document):
    pass
