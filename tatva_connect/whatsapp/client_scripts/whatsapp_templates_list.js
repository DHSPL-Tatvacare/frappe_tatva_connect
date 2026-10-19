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

		if (!frappe.model.can_write("WhatsApp Account")) {
			return;
		}
		// A refresh reads one account's provider, so the account is picked first (prefilled when there is one).
		listview.page.add_inner_button(__("Refresh From WhatsApp"), () =>
			tatva_record_first_dialog({
				doctype: "WhatsApp Account", fieldname: "account", label: __("WhatsApp Account"), filters: { status: "Active" },
				empty_title: __("No WhatsApp Account"),
				empty_message: __("Add a WhatsApp Account first. Its provider is where the templates come from."),
				title: __("Refresh From WhatsApp"),
				intro: __("Pick the account whose templates to refresh. Only that account is read."),
				primary_label: __("Refresh"),
				on_submit: (dialog, { account }) => {
					dialog.hide();
					tatva_wa_refresh_templates(account, () => listview.refresh());
				},
			})
		);
	};

	frappe.listview_settings["WhatsApp Templates"] = settings;
})();
