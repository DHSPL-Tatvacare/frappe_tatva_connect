# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A configuration record's edit history — Task Forms and Workflows read it through this one call.

The rows are the Lead Activity rail's own: Frappe's `Version` records read by `lead_events.recent_versions` and
shaped by `api.activities._version_row`, closed by `lead_events.creation_event`. Nothing is worded twice.
A Publish freezes the whole record, so each publish row says what changed since the publish before: read off the
two frozen snapshots, never a draft in between. A workflow tracks no field edits, so its publishes are its history.
"""
import frappe
from frappe import _

from tatva_connect.activity.lead_events import VERSION_WINDOW, creation_event, recent_versions
from tatva_connect.api.activities import _version_row
from tatva_connect.workflow_engine import registry

# A record whose Publish freezes a snapshot: its version doctype and the field that links a version back to it.
PUBLISHED = {
	"CRM Workflow": ("CRM Workflow Version", "workflow"),
	"CRM Task Type": ("CRM Task Type Version", "task_type"),
}


@frappe.whitelist()
def change_history(doctype, name):
	"""Who changed what on this record, newest first, as rail rows; gated on reading the record, as Desk's own history is."""
	frappe.has_permission(doctype, "read", name, throw=True)
	rows = [row for row in (_version_row(v, doctype) for v in recent_versions(doctype, name)) if row]
	if doctype in PUBLISHED:
		rows += _publishes(doctype, name)
	rows = sorted(rows, key=lambda row: row["creation"], reverse=True)[:VERSION_WINDOW]
	return [*rows, {"name": f"creation:{name}", **creation_event(doctype, name)}]


def _publishes(doctype, name):
	"""The latest publishes as rail rows, each with one line per part added or removed and per setting changed since the
	publish before. Read after the record's own read check above."""
	version_doctype, link = PUBLISHED[doctype]
	versions = frappe.get_all(
		version_doctype, filters={link: name},
		fields=["name", "owner", "creation", "version_no", "payload_json"],
		order_by="version_no desc", limit=VERSION_WINDOW + 1,
	)[::-1]
	parts = [_parts(doctype, frappe.parse_json(v.payload_json or "{}")) for v in versions]
	# The oldest read is only the baseline, unless it is the first version ever.
	first = 0 if len(versions) <= VERSION_WINDOW else 1
	return [
		{
			"name": versions[i].name, "activity_type": "published", "creation": versions[i].creation,
			"owner": versions[i].owner, "version_no": versions[i].version_no,
			"changes": _diff(parts[i - 1] if i else {}, parts[i]),
		}
		for i in range(first, len(versions))
	]


def _parts(doctype, payload):
	"""A snapshot as {key: (title, {setting label: value})}; the record's own fields are the part keyed "", titled "", never added or removed."""
	if doctype == "CRM Workflow":
		return {n["node_id"]: (_("Step {0}").format(n["node_id"]), _step_settings(n)) for n in payload.get("nodes") or []}
	meta = frappe.get_meta(doctype)
	tables = {df.fieldname: df for df in meta.get_table_fields()}
	out = {"": ("", {_(meta.get_label(k)): v for k, v in payload.items() if k not in tables})}
	for fieldname, df in tables.items():
		child = frappe.get_meta(df.options)
		for i, row in enumerate(payload.get(fieldname) or [], start=1):
			title = row.get("label") or row.get("rule_label") or _("Row {0}").format(i)
			out[(fieldname, row.get("fieldname") or i)] = (f"{_(df.label)} · {title}", {_(child.get_label(k)): v for k, v in row.items()})
	return out


def _step_settings(node):
	"""A step's settings under the labels its inspector shows, plus where it leads."""
	declared = registry.config_fields(node["node_type"]) if node.get("node_type") in registry.NODE_TYPES else []
	labels = {f["name"]: f["label"] for f in declared}
	settings = {_(labels.get(k, k)): v for k, v in (frappe.parse_json(node.get("config_json") or "{}") or {}).items()}
	settings[_("Type")] = node.get("node_type")
	settings[_("Next steps")] = ", ".join(f"{e.get('output')}: {e.get('to')}" for e in node.get("edges") or [])
	return settings


def _diff(before, after):
	lines = [{"label": _("Added"), "from": "", "to": after[k][0]} for k in after if k and k not in before]
	lines += [{"label": _("Removed"), "from": before[k][0], "to": ""} for k in before if k and k not in after]
	for k in (k for k in after if k in before):
		# Named as it reads now: a renamed question is found under its new label.
		(_was, old), (title, new) = before[k], after[k]
		for label in dict.fromkeys([*old, *new]):
			if old.get(label) != new.get(label):
				lines.append({"label": f"{title} · {label}" if title else label, "from": _shown(old.get(label)), "to": _shown(new.get(label))})
	return lines


def _shown(value):
	if value in (None, "", [], {}):
		return ""
	return value if isinstance(value, str) else frappe.as_json(value, indent=None)
