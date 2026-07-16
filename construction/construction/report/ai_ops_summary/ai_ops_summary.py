# Copyright (c) 2026, BuildCost Pro and contributors
# For license information, please see license.txt
#
# F1: AI ops visibility — spend, failure rate, error mix, and latency per
# tool, so "tool X is failing 40% of the time" is visible before users
# complain in reviews. Run from Desk (Report: AI Ops Summary) or:
#   bench --site construction.local execute \
#     construction.construction.report.ai_ops_summary.ai_ops_summary.execute

import frappe
from frappe.utils import add_days, nowdate


def execute(filters=None):
    filters = filters or {}
    from_date = filters.get("from_date") or add_days(nowdate(), -7)
    to_date = filters.get("to_date") or nowdate()

    rows = frappe.db.sql(
        """
        select
            job_type                                            as tool,
            count(*)                                            as jobs,
            sum(status = 'succeeded')                           as succeeded,
            sum(status = 'failed')                              as failed,
            sum(error_code in ('SAFETY_BLOCKED',
                               'MODERATION_REJECTED'))          as content_blocked,
            sum(error_code like 'VENDOR%%')                     as vendor_errors,
            coalesce(sum(cost_cents), 0)                        as spend_cents,
            round(avg(case
                when started_at is not null and completed_at is not null
                then timestampdiff(second, started_at, completed_at)
            end), 1)                                            as avg_latency_s
        from `tabAI Job`
        where creation >= %(from_date)s
          and creation < date_add(%(to_date)s, interval 1 day)
        group by job_type
        order by jobs desc
        """,
        {"from_date": from_date, "to_date": to_date},
        as_dict=True,
    )

    for r in rows:
        jobs = r["jobs"] or 0
        r["failure_pct"] = round(100.0 * (r["failed"] or 0) / jobs, 1) if jobs else 0

    columns = [
        {"fieldname": "tool", "label": "Tool", "fieldtype": "Data", "width": 120},
        {"fieldname": "jobs", "label": "Jobs", "fieldtype": "Int", "width": 70},
        {"fieldname": "succeeded", "label": "OK", "fieldtype": "Int", "width": 70},
        {"fieldname": "failed", "label": "Failed", "fieldtype": "Int", "width": 80},
        {"fieldname": "failure_pct", "label": "Fail %", "fieldtype": "Float", "width": 80},
        {"fieldname": "vendor_errors", "label": "Vendor errs", "fieldtype": "Int", "width": 100},
        {"fieldname": "content_blocked", "label": "Safety/mod blocks", "fieldtype": "Int", "width": 130},
        {"fieldname": "spend_cents", "label": "Spend (¢)", "fieldtype": "Int", "width": 90},
        {"fieldname": "avg_latency_s", "label": "Avg latency (s)", "fieldtype": "Float", "width": 110},
    ]
    return columns, rows
