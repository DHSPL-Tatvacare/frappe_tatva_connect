// Bulk-replay a provider's dead-letter queue from the Desk list.
//
// Two things are replayable: Failed (the worker raised) and Cancelled (declined on purpose). The
// second is the one reached for after fixing configuration -- map a DID, then replay everything that
// was dropped for want of it. An in-flight (Queued) delivery is never re-run.
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

		listview.page.add_inner_button(__("Replay Deliveries"), () => {
			const dialog = new frappe.ui.Dialog({
				title: __("Replay Deliveries"),
				fields: [
					{
						fieldname: "service",
						fieldtype: "Data",
						label: __("Service"),
						reqd: 1,
						description: __("As it appears in the Service column, e.g. Acefone or WATI."),
					},
					{
						fieldname: "status",
						fieldtype: "Select",
						label: __("Status"),
						options: ["Failed", "Cancelled"].join("\n"),
						default: "Failed",
						reqd: 1,
						description: __("Failed = the worker raised. Cancelled = declined on purpose; replay these after mapping a DID or adding a capture rule."),
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
						method: "tatva_connect.webhooks.spine.replay_service",
						args: values,
						freeze: true,
						freeze_message: __("Re-enqueuing…"),
						callback: (r) => {
							dialog.hide();
							frappe.msgprint({
								title: __("Replay queued"),
								message: __("{0} delivery(s) re-enqueued.", [r.message || 0]),
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
