// Copyright (c) 2026, TatvaCare and contributors
// For license information, please see license.txt

// Desk Client Script (CRM Maps Settings) — "Check Connection" button.
// Reverse-geocodes a fixed sample point via the saved key and shows the resolved address,
// so an operator can confirm the Google key + billing + Geocoding API before going live.
// Seeded by tatva_connect/client_scripts_seed.py. Mirrors crm_azure_storage_settings.js.

frappe.ui.form.on("CRM Maps Settings", {
	check_connection(frm) {
		frappe.call({
			method: "tatva_connect.location.api.test_connection",
			freeze: true,
			freeze_message: __("Testing Google Maps…"),
			callback: (r) => {
				if (r.message) {
					frappe.msgprint({ title: __("Success"), message: r.message, indicator: "green" });
				}
			},
		});
	},
});
