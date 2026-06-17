"""Project the lead's full action history into ONE clean Activity-tab audit.

Frappe CRM's native timeline (crm.api.activities.get_activities) is built from version history +
comments + emails + attachments — linked tasks and logged activities never enter it, and a logged
activity's documents/location land as separate, unlinked rows. This override is the single audit
assembler: it folds each logged activity's documents + captured location INTO one chained entry,
relabels stage changes legibly, and drops derived/auto-synced field noise — so the timeline reads
as a per-lead audit. Derived on read, nothing stored. Registered via override_whitelisted_methods.
"""
import frappe

from crm.api.activities import get_activities as _native_get_activities

from tatva_connect.activity.api import _blob_key, lead_timeline

# Lead field-changes we never surface in the audit: derived (custom_main_stage follows custom_stage)
# or auto-synced headline mirrors (the latest-lab snapshot). Keeps the signal, drops the churn.
_NOISE_FIELDS = {
	"custom_main_stage",
	"custom_latest_hba1c", "custom_latest_fbs",
	"custom_height_feet", "custom_weight_kg", "custom_last_report_date",
}


def _full_name(user):
	return (user and frappe.db.get_value("User", user, "full_name")) or user


def _stage_label(pk):
	"""A CRM Lead Stage PK ({program}::{stage}) -> its human stage name, or the PK if unknown."""
	return (pk and frappe.db.get_value("CRM Lead Stage", pk, "stage")) or pk or ""


def _activity_events(entries):
	"""One chained audit row per logged activity — status, location and documents folded in."""
	return [{
		"activity_type": "activity_logged",
		"creation": e["creation"],
		"owner": e["owner"],
		"owner_name": e["owner_name"],
		"verb": "completed" if e.get("done") else "logged",
		"subject": e["activity_type"],
		"status": e.get("status") or "",
		"location": e.get("location"),
		"documents": e.get("documents") or [],
		"is_automation": False,
	} for e in entries]


def _task_events(tasks):
	"""Created/closed rows for plain (non-activity) tasks."""
	events = []
	for t in tasks:
		who = t.get("assigned_to") or t.get("owner")
		base = {"owner": who, "owner_name": _full_name(who), "subject": t.get("title")}
		events.append({"activity_type": "task_created", "creation": str(t.get("creation")), **base})
		if (t.get("status") or "").lower() in ("done", "completed"):
			events.append({"activity_type": "task_closed", "creation": str(t.get("modified")), **base})
	return events


def _stage_moved(a):
	"""A native custom_stage change -> a legible stage_moved row (PKs resolved to stage names)."""
	d = a.get("data") or {}
	return {
		"activity_type": "stage_moved",
		"creation": a["creation"],
		"owner": a["owner"],
		"owner_name": _full_name(a["owner"]),
		"from_stage": _stage_label(d.get("old_value")),
		"to_stage": _stage_label(d.get("value")),
		"is_automation": False,
	}


def _curate_native(activities, doc_keys):
	"""Keep the native rows that belong in the audit; transform/drop the rest:
	- attachment_log for a file folded into an activity -> dropped (shown inside that activity);
	- custom_stage change -> a clean stage_moved row;
	- derived / auto-synced field noise -> dropped.
	"""
	out = []
	for a in activities:
		at = a.get("activity_type")
		d = a.get("data") or {}
		if at == "attachment_log":
			if _blob_key(d.get("file_url")) not in doc_keys:
				out.append(a)
		elif at in ("changed", "added", "removed"):
			field = d.get("field")
			if field == "custom_stage":
				out.append(_stage_moved(a))
			elif field not in _NOISE_FIELDS:
				out.append(a)
		else:
			out.append(a)
	return out


@frappe.whitelist()
def get_activities(name: str):
	activities, calls, notes, tasks, attachments = _native_get_activities(name)
	logged = lead_timeline(name)
	logged_names = {e["name"] for e in logged}
	plain_tasks = [t for t in tasks if t.get("name") not in logged_names]
	doc_keys = {_blob_key(doc["file_url"]) for e in logged for doc in (e.get("documents") or [])}
	doc_keys.discard("")

	activities = (
		_curate_native(list(activities), doc_keys)
		+ _activity_events(logged)
		+ _task_events(plain_tasks)
	)
	activities.sort(key=lambda x: str(x["creation"]), reverse=True)
	return activities, calls, notes, tasks, attachments
