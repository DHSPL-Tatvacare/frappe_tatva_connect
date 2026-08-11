// Every automation ships dormant, so an automation that is off does nothing and logs nothing. The one state
// nobody can see is a switch ticked ON above a dormant parent: the row reads armed, the gate answers off, and
// there is no trace anywhere. This paints the derived status onto the list the operator already uses, and says
// so at the top when a chain is broken, so the silent case is met before an automation is built on top of it.
//
// The existing listview_settings is EXTENDED, not replaced -- the doctype may pick one up later, and assigning
// over the object would silently drop it.
//
// THE DERIVED STATUS IS REVALIDATED, NEVER SNAPSHOTTED. It used to be fetched once in `onload` into a
// module-level map that `get_indicator` then read for the life of the page: ticking a switch showed a success
// toast and left the pill reading Off until a hard reload, while the engine (`settings.is_enabled`, a fresh
// read per call) had already changed behaviour. Stale-while-revalidate with only the stale half, in a feature
// built to stop switches lying about their state. `settings.refresh` is frappe's own hook for the other half:
// base_list.js:552 calls it after every list refresh, which is the same moment the rows themselves are known
// to be fresh. `settings.before_render` (list_view.js:658) was rejected -- it is synchronous and is handed no
// listview, so it can neither await the read nor repaint the rows it would have to correct.
// `settings.onload` (list_view.js:366) is rejected for the same class of reason and is why this once painted a
// BLANK list: it runs inside init(), before the first refresh() (base_list.js:15-17), so a read started there
// resolves while `listview.data` is still [] and the repaint below cleared every row and re-appended nothing.

(() => {
	const DOCTYPE = "CRM Tatva Automation";
	const settings = frappe.listview_settings[DOCTYPE] || {};
	const upstream_refresh = settings.refresh;

	// The last answer, painted while the next one is in flight so a row is never blank waiting on this. It is
	// a render cache, not a page-lifetime snapshot: every list refresh replaces it.
	let status_by_key = {};
	// The banner element `add_inner_message` hands back, kept so a chain that has been REPAIRED can clear it.
	let broken_message = null;
	let in_flight = null;
	// A revalidate asked for while one is already in the air, remembered so the answer it wanted is not lost.
	let again = false;

	function revalidate(listview) {
		// One read in the air at a time: a filter change and a realtime update can both land in the same tick. The second is REMEMBERED, never dropped -- dropped, it left the pills showing the answer from before the tick that had just been written.
		if (in_flight) {
			again = true;
			return in_flight;
		}
		in_flight = frappe
			.call({ method: "tatva_connect.automation.status.switch_state" })
			.then((r) => {
				const rows = (r && r.message) || [];
				const next = {};
				rows.forEach((row) => {
					next[row.key] = row;
				});
				const changed = JSON.stringify(next) !== JSON.stringify(status_by_key);
				status_by_key = next;
				announce_broken(listview, rows);
				// Repaint only when the answer moved AND there are rows to repaint: `render_list` clears every row container and re-appends from `listview.data` (list_view.js:677), so painting an empty set leaves a blank body under a live header, and it never calls `toggle_result_area`, so not even the no-results panel shows.
				if (changed && listview.data && listview.data.length) listview.render_list();
			})
			.finally(() => {
				in_flight = null;
				if (again) {
					again = false;
					revalidate(listview);
				}
			});
		return in_flight;
	}

	settings.refresh = function (listview) {
		if (upstream_refresh) {
			upstream_refresh(listview);
		}
		revalidate(listview);
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
		// `add_inner_message` only REPLACES when called again, so a repaired chain must remove its own banner.
		if (!broken.length) {
			if (broken_message) broken_message.remove();
			broken_message = null;
			return;
		}
		const named = broken
			.map((row) => `${frappe.utils.escape_html(row.key)} &rarr; ${frappe.utils.escape_html(row.blocked_by)}`)
			.join(", ");
		broken_message = listview.page.add_inner_message(
			`<span class="text-danger">${__("Switched on but not running, because what each one leans on is off")}: ${named}</span>`
		);
	}

	frappe.listview_settings[DOCTYPE] = settings;
})();
