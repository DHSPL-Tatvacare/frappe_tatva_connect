// Copyright (c) 2026, TatvaCare and contributors
// For license information, please see license.txt

frappe.ui.form.on("CRM Azure Storage Settings", {
	check_connection(frm) {
		frappe.call({
			method: "tatva_connect.storage.api.test_connection",
			freeze: true,
			freeze_message: __("Testing Azure connection…"),
			callback: (r) => {
				if (r.message) {
					frappe.msgprint({ title: __("Success"), message: r.message, indicator: "green" });
				}
			},
		});
	},
});
