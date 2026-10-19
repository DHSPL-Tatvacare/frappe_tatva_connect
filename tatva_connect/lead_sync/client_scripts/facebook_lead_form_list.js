// Desk Client Script — Facebook Lead Form list: discover newly published forms from where you look for one.

(() => {
	const settings = frappe.listview_settings["Facebook Lead Form"] || {};
	const upstream_onload = settings.onload;

	settings.onload = function (listview) {
		// Extended, never replaced: assigning over the object drops whatever the doctype already ships.
		if (upstream_onload) {
			upstream_onload(listview);
		}
		if (!frappe.model.can_write("CRM Facebook App")) {
			return;
		}

		// Discovery runs on one app's token, so the app is picked first (prefilled when there is one).
		listview.page.add_inner_button(__("Refresh From Facebook"), () =>
			tatva_fb_app_dialog({
				title: __("Refresh From Facebook"),
				intro: tatva_fb_discovery_prompt(),
				primary_label: __("Refresh"),
				on_submit: (dialog, values) => {
					dialog.hide();
					tatva_fb_discover(values.facebook_app, () => listview.refresh());
				},
			})
		);
	};

	frappe.listview_settings["Facebook Lead Form"] = settings;
})();
