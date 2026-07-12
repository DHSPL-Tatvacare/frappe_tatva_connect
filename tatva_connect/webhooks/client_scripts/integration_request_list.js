// Bulk-replay a provider's dead-letter queue from the Desk list.
//
// `replay_failed` re-enqueues only rows the worker actually raised on, so an in-flight delivery is
// never re-run. The provider is asked for rather than guessed: the list carries several.
//
// The existing listview_settings is EXTENDED, not replaced. Frappe ships its own onload for this
// doctype, which renders the log-retention banner; assigning over the object would silently drop it.

(() => {
	const settings = frappe.listview_settings["Integration Request"] || {};
	const upstream_onload = settings.onload;

	settings.onload = function (listview) {
		if (upstream_onload) {
			upstream_onload(listview);
		}
		if (!frappe.user.has_role("System Manager")) {
			return;
		}

		listview.page.add_inner_button(__("Replay Failed"), () => {
			const dialog = new frappe.ui.Dialog({
				title: __("Replay Failed Deliveries"),
				fields: [
					{
						fieldname: "service",
						fieldtype: "Data",
						label: __("Service"),
						reqd: 1,
						description: __("As it appears in the Service column, e.g. Acefone or WATI."),
					},
					{
						fieldname: "since",
						fieldtype: "Datetime",
						label: __("Since"),
						description: __("Optional. Only deliveries created on or after this time."),
					},
				],
				primary_action_label: __("Replay"),
				primary_action(values) {
					frappe.call({
						method: "tatva_connect.webhooks.spine.replay_failed",
						args: values,
						freeze: true,
						freeze_message: __("Re-enqueuing…"),
						callback: (r) => {
							dialog.hide();
							frappe.msgprint({
								title: __("Replay queued"),
								message: __("{0} failed delivery(s) re-enqueued.", [r.message || 0]),
								indicator: "green",
							});
							listview.refresh();
						},
					});
				},
			});
			dialog.show();
		});
	};

	frappe.listview_settings["Integration Request"] = settings;
})();
