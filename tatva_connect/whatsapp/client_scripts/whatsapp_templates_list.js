// Desk Client Script — WhatsApp Templates list: refresh ONE account's templates, picked here, instead of upstream's every-account Sync from Meta.

(() => {
	const settings = frappe.listview_settings["WhatsApp Templates"] || {};
	const upstream_onload = settings.onload;

	settings.onload = function (listview) {
		// Extended, never replaced: assigning over the object drops whatever the doctype already ships.
		if (upstream_onload) {
			upstream_onload(listview);
		}
		// Upstream's button syncs every account and names Meta; templates come through each account's own provider.
		listview.page.remove_inner_button(__("Sync from Meta"));

		listview.page.add_inner_button(__("Refresh From WhatsApp"), () => {
			frappe.db.get_list("WhatsApp Account", { fields: ["name"], limit: 0 }).then((accounts) => {
				if (!accounts || !accounts.length) {
					frappe.msgprint({
						title: __("No WhatsApp Account"),
						indicator: "orange",
						message: __("Add a WhatsApp Account first. Its provider is where the templates come from."),
					});
					return;
				}
				if (accounts.length === 1) {
					frappe.confirm(__("Refresh the templates of {0}?", [accounts[0].name]), () =>
						tatva_wa_refresh_templates(accounts[0].name, () => listview.refresh())
					);
					return;
				}
				const dialog = new frappe.ui.Dialog({
					title: __("Refresh From WhatsApp"),
					fields: [
						{ fieldtype: "HTML", options: `<p class="text-muted">${__("Pick the account whose templates to refresh. Only that account is read.")}</p>` },
						{ fieldname: "account", fieldtype: "Link", options: "WhatsApp Account", label: __("WhatsApp Account"), reqd: 1 },
					],
					primary_action_label: __("Refresh"),
					primary_action(values) {
						dialog.hide();
						tatva_wa_refresh_templates(values.account, () => listview.refresh());
					},
				});
				dialog.show();
			});
		});
	};

	frappe.listview_settings["WhatsApp Templates"] = settings;
})();
