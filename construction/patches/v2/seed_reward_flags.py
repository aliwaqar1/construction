# Seed Feature Flag rows for the G1/G2 reward levers. Idempotent.
#
# rewarded_ads   - G1: "watch an ad for +1 credit" (v1.ad_reward)
# share_rewards  - G2: "+1 credit after sharing a design" (v1.share_reward)
#
# Both ship DISABLED: they are monetization levers the owner flips on when
# ready (G1 additionally needs a real AdMob rewarded unit id in the app
# build). The client also defaults both flags off.

import frappe

_FLAGS = {
    "rewarded_ads": "G1: rewarded ad grants +1 AI credit (1/user/day).",
    "share_rewards": "G2: sharing a design grants +1 AI credit (1/user/day).",
}


def execute():
    if not frappe.db.exists("DocType", "Feature Flag"):
        return
    for flag_key, description in _FLAGS.items():
        if frappe.db.exists("Feature Flag", flag_key):
            continue
        doc = frappe.new_doc("Feature Flag")
        doc.flag_key = flag_key
        doc.enabled = 0
        doc.description = description
        doc.insert(ignore_permissions=True)
    frappe.db.commit()
