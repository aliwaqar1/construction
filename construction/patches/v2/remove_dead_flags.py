# Delete Feature Flag rows whose features no longer exist. Idempotent.
#
# Replaces patches/v2/seed_reward_flags.py, which created the first two.
#
#   rewarded_ads   - gated v1.ad_reward, removed. Nothing proved an ad had
#                    been watched (no AdMob SSV), so the route paid a credit
#                    a day to any client that could send a POST.
#   share_rewards  - gated v1.share_reward, removed. A share is unverifiable
#                    by construction.
#   credit_gating  - gated whether AI submits debited the ledger at all.
#                    Submits now always debit; the flag was the single row
#                    whose absence made every paid generation free, and it
#                    was never seeded, so it read as "off" wherever nobody
#                    had created it by hand.
#
# A stale row is not merely untidy: it shows up in the desk as a switch an
# operator can reasonably believe still does something.

import frappe

_DEAD_FLAGS = ("rewarded_ads", "share_rewards", "credit_gating")


def execute():
    if not frappe.db.exists("DocType", "Feature Flag"):
        return
    removed = []
    for flag_key in _DEAD_FLAGS:
        if not frappe.db.exists("Feature Flag", flag_key):
            continue
        frappe.delete_doc(
            "Feature Flag", flag_key, ignore_permissions=True, force=True
        )
        removed.append(flag_key)
    if removed:
        frappe.db.commit()
        frappe.logger().info(
            "remove_dead_flags: deleted %s" % ", ".join(removed)
        )
