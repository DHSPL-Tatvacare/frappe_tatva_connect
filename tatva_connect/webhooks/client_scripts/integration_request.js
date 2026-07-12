// Replay a stored webhook delivery from the Desk.
//
// Both endpoints already exist and are already System-Manager-gated; this only puts them on the
// form. A Cancelled row is the interesting one: it was dropped on purpose (an unmapped DID, or no
// capture rule), so mapping what the outcome names and replaying the row lands the call.

frappe.ui.form.on("Integration Request", {
	refresh(frm) {
		if (frm.is_new() || !frappe.user.has_role("System Manager")) {
			return;
		}

		const replayable = ["Failed", "Cancelled", "Completed"].includes(frm.doc.status);
		if (!replayable) {
			return;
		}

		frm.add_custom_button(__("Replay"), () => {
			frappe.confirm(
				__("Re-run this delivery through the webhook worker?"),
				() => {
					frappe.call({
						method: "tatva_connect.webhooks.spine.replay",
						args: { integration_request: frm.doc.name },
						freeze: true,
						freeze_message: __("Replaying…"),
						callback: () => {
							frappe.show_alert({ message: __("Replayed"), indicator: "green" });
							frm.reload_doc();
						},
					});
				}
			);
		});

		if (frm.doc.status === "Failed") {
			frm.set_intro(__("This delivery failed. The traceback is in Error below."), "red");
		} else if (frm.doc.status === "Cancelled") {
			frm.set_intro(
				__("This delivery was logged but not processed, on purpose. See Output for why."),
				"orange"
			);
		}
	},
});
