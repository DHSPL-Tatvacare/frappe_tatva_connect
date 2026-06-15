"""Inject CRM Task lifecycle + logged activities into the Lead/Deal activity timeline.

Frappe CRM's native timeline (crm.api.activities.get_activities) is built from version
history + comments + emails + attachments. Linked Tasks never enter it. LSQ's feed shows
task lifecycle AND logged activities, so we derive synthetic entries from the Task rows.

Every entry uses the crm fallback renderer's full contract — owner_name + type (verb) +
data.field_label (bold subject) + value — so each renders as a complete, legible line
(the old terse entries collapsed to a fragment). Logged activities come from the SINGLE
projection activity.api.lead_timeline (one brain with the Desk timeline); plain tasks keep
their created/closed pair. Nothing is stored. Registered via override_whitelisted_methods.
"""

import frappe
from crm.api.activities import get_activities as _native_get_activities

from tatva_connect.activity.api import lead_timeline
from tatva_connect.location.api import lead_captures


def _activity_events(entries):
	"""Logged-activity entries from the shared lead_timeline projection."""
	return [{
		"activity_type": "activity_logged",
		"creation": e["creation"],
		"owner": e["owner"],
		"owner_name": e["owner_name"],
		"type": "logged",
		"data": {"field_label": e["activity_type"]},
		"value": e["status"] + (" · " + e["address"] if e.get("address") else ""),
	} for e in entries]


def _map_link_events(captures):
	"""Clickable "View on map" affordance for in-person captures. The crm fallback renderer binds a
	real <a href> ONLY for the attachment_log shape (data.file_url / data.file_name) — so we reuse it
	(native, not a fork). file_url is a keyless Google Maps URL opening the interactive map in a new tab."""
	out = []
	for c in captures:
		if not (c.get("lat") and c.get("lng")):
			continue
		out.append({
			"activity_type": "attachment_log",
			"creation": c["when"],
			"owner": c["owner"],
			"owner_name": c["rep"],
			"data": {
				"type": "captured location",
				"file_name": c["address"] or "View on map",
				"file_url": "https://www.google.com/maps?q={0},{1}".format(c["lat"], c["lng"]),
			},
		})
	return out


def _task_events(tasks):
	"""Created/closed entries for plain (non-activity) tasks, full-line contract."""
	events = []
	for t in tasks:
		owner = t.get("assigned_to") or t.get("owner")
		owner_name = (owner and frappe.db.get_value("User", owner, "full_name")) or owner
		title = t.get("title")
		events.append({
			"activity_type": "task_created",
			"creation": str(t.get("creation")),
			"owner": owner,
			"owner_name": owner_name,
			"type": "created task",
			"data": {"field_label": title},
		})
		if (t.get("status") or "").lower() in ("done", "completed"):
			events.append({
				"activity_type": "task_closed",
				"creation": str(t.get("modified")),
				"owner": owner,
				"owner_name": owner_name,
				"type": "completed task",
				"data": {"field_label": title},
			})
	return events


@frappe.whitelist()
def get_activities(name: str):
	activities, calls, notes, tasks, attachments = _native_get_activities(name)
	logged = lead_timeline(name)
	activity_task_names = {e["name"] for e in logged}
	plain_tasks = [t for t in tasks if t.get("name") not in activity_task_names]
	activities = (
		list(activities)
		+ _activity_events(logged)
		+ _task_events(plain_tasks)
		+ _map_link_events(lead_captures(name))
	)
	activities.sort(key=lambda x: str(x["creation"]), reverse=True)
	return activities, calls, notes, tasks, attachments
