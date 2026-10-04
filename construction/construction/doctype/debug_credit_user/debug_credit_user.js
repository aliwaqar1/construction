// Copyright (c) 2026, ali waqar and contributors
// For license information, please see license.txt

frappe.ui.form.on("Debug Credit User", {
	refresh(frm) {
		if (frm.is_new()) return;
		frm.add_custom_button(__("Grant Credits"), () => {
			frappe.prompt(
				{ fieldname: "amount", fieldtype: "Int", label: __("Credits"), default: 50, reqd: 1 },
				({ amount }) =>
					frappe.call({
						method: "construction.construction.doctype.debug_credit_user.debug_credit_user.grant_credits",
						args: { name: frm.doc.name, amount },
						freeze: true,
						callback: (r) =>
							frappe.show_alert({
								message: __("Granted {0} credits. Balance: {1}", [amount, r.message]),
								indicator: "green",
							}),
					}),
				__("Grant Credits"),
				__("Grant")
			);
		});
	},
});
