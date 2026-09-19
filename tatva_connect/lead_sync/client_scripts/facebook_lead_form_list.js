// Desk Client Script — Facebook Lead Form list: discover newly published forms from where you look for one.

(() => {
	const settings = frappe.listview_settings["Facebook Lead Form"] || {};
	const upstream_onload = settings.onload;

	settings.onload = function (listview) {
		// Extended, never replaced: assigning over the object drops whatever the doctype already ships.
		if (upstream_onload) {
			upstream_onload(listview);
		}
		if (!frappe.user.has_role("System Manager")) {
			return;
		}

		listview.page.add_inner_button(__("Refresh From Facebook"), () => {
			frappe.db.get_list("CRM Facebook App", { fields: ["name", "app_name"], limit: 0 }).then((apps) => {
				if (!apps || !apps.length) {
					frappe.msgprint({
						title: __("No Facebook App"),
						indicator: "orange",
						message: __("Add a Facebook App first — it holds the credential Facebook is asked with."),
					});
					return;
				}
				if (apps.length === 1) {
					frappe.confirm(tatva_fb_discovery_prompt(), () =>
						tatva_fb_discover(apps[0].name, () => listview.refresh())
					);
					return;
				}
				// Discovery is scoped to one app's token, so several apps means choosing which one.
				const dialog = new frappe.ui.Dialog({
					title: __("Refresh From Facebook"),
					fields: [
						{ fieldtype: "HTML", options: `<p class="text-muted">${tatva_fb_discovery_prompt()}</p>` },
						{
							fieldname: "app",
							fieldtype: "Link",
							options: "CRM Facebook App",
							label: __("Facebook App"),
							reqd: 1,
						},
					],
					primary_action_label: __("Refresh"),
					primary_action(values) {
						dialog.hide();
						tatva_fb_discover(values.app, () => listview.refresh());
					},
				});
				dialog.show();
			});
		});
	};

	frappe.listview_settings["Facebook Lead Form"] = settings;
})();
