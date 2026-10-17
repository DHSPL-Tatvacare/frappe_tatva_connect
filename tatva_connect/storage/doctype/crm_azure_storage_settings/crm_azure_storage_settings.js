// Copyright (c) 2026, TatvaCare and contributors
// For license information, please see license.txt

frappe.ui.form.on("CRM Azure Storage Settings", {
	refresh(frm) {
		// frappe appends a headline on every call, so the last one is removed first, as its own set_intro does.
		if (frm.__headline) frm.__headline.remove();
		frm.__headline = frm.dashboard.set_headline_alert(
			__("These settings shape how the CRM stores files in Azure. Files move to Azure only while the Storage::Azure::offload feature flag is on. The form sets how long a download link works and whether the app server keeps its copy. The site config holds the Azure connection string."),
			"blue",
			true
		);
	},
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
