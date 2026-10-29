// Copyright (c) 2026, TatvaCare and contributors
// For license information, please see license.txt

frappe.ui.form.on("Atlassian Settings", {
	check_connection(frm) {
		frm.call({ method: "check_connection", doc: frm.doc, freeze: true, freeze_message: __("Checking Atlassian…") }).then((r) => {
			if (r.message) frappe.msgprint({ title: __("Connected"), message: r.message, indicator: "green" });
		});
	},
});
