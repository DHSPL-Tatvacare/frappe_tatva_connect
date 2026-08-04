// Every automation ships dormant, so an automation that is off does nothing and logs nothing. The one state
// nobody can see is a switch ticked ON above a dormant parent: the row reads armed, the gate answers off, and
// there is no trace anywhere. This paints the derived status onto the list the operator already uses, and says
// so at the top when a chain is broken, so the silent case is met before an automation is built on top of it.
//
// The existing listview_settings is EXTENDED, not replaced -- the doctype may pick one up later, and assigning
// over the object would silently drop it.

(() => {
	const DOCTYPE = "CRM Tatva Automation";
	const settings = frappe.listview_settings[DOCTYPE] || {};
	const upstream_onload = settings.onload;

	// The derived status per key, filled once per list view; until it arrives every row falls through to
	// frappe's own enabled/disabled pill, so the list is never blank waiting on this.
	const status_by_key = {};

	settings.onload = function (listview) {
		if (upstream_onload) {
			upstream_onload(listview);
		}
		frappe.call({ method: "tatva_connect.automation.status.switch_state" }).then((r) => {
			const rows = (r && r.message) || [];
			rows.forEach((row) => {
				status_by_key[row.key] = row;
			});
			announce_broken(listview, rows);
			listview.render_list();
		});
	};

	settings.get_indicator = function (doc) {
		const row = status_by_key[doc.name];
		if (!row) return null;
		if (row.status === "broken_dependency") {
			return [__("Broken dependency"), "red", "enabled,=,1"];
		}
		if (row.status === "on") {
			return [__("On"), "green", "enabled,=,1"];
		}
		return [__("Off"), "gray", "enabled,=,0"];
	};

	function announce_broken(listview, rows) {
		const broken = rows.filter((row) => row.status === "broken_dependency");
		if (!broken.length) return;
		const named = broken
			.map((row) => `${frappe.utils.escape_html(row.key)} &rarr; ${frappe.utils.escape_html(row.blocked_by)}`)
			.join(", ");
		listview.page.add_inner_message(
			`<span class="text-danger">${__("Switched on but not running, because what each one leans on is off")}: ${named}</span>`
		);
	}

	frappe.listview_settings[DOCTYPE] = settings;
})();
