"""TEST-ONLY seed — two standard Smart Views + their catalog rows, for the P0 headless proof.

This is NOT business data and NOT a fixture: it ships under tests/, is idempotent, and is only
ever run by the proof harness (run_proof) on a dev bench. Business catalog rows + real views ship
as db-seeds the operator runs by hand (CLAUDE.md A5). The catalog rows seeded here describe the
9 promoted CRM Task columns + a display-only payload field + a couple of CRM Lead parent fields,
so the composer has something to project, filter and sort on the unified activity model.
"""
import frappe

# (field_key, label, fieldname, sql_source, child_pick, filterable, sortable, surface, applies_to)
_CATALOG = [
	# Lead parent fields
	("lead:first_name", "First Name", "first_name", "parent", None, 1, 1, "worklist", "lead"),
	("lead:status", "Status", "status", "parent", None, 1, 1, "worklist", "lead"),
	("lead:mobile_no", "Mobile", "mobile_no", "parent", None, 1, 0, "worklist", "lead"),
	# Activity (Order Punch) — the 9 promoted CRM Task columns (filter/sort)
	("act:title", "Title", "title", "task", None, 0, 1, "worklist", "activity:Order Punch Status"),
	("act:status", "Status", "status", "task", None, 1, 1, "worklist", "activity:Order Punch Status"),
	("act:outcome", "Outcome", "custom_outcome", "task", None, 1, 1, "worklist", "activity:Order Punch Status"),
	("act:reference", "Order ID", "custom_reference", "task", None, 1, 1, "worklist", "activity:Order Punch Status"),
	("act:scheduled", "Shipped By", "custom_scheduled_at", "task", None, 1, 1, "worklist", "activity:Order Punch Status"),
	# Activity (Order Punch) — a display-only payload field (JSON_EXTRACT, not filter/sort)
	("act:cycle", "Cycle", "cycle_category", "payload", None, 0, 0, "worklist", "activity:Order Punch Status"),
]

_FIELDS = [
	"field_key", "label", "fieldname", "sql_source",
	"child_pick", "filterable", "sortable", "surface", "applies_to",
]

TEST_ACTIVITY_TYPE = "Order Punch Status"
LEAD_VIEW = "TEST Smart View — Leads"
ACT_VIEW = "TEST Smart View — Order Punch"


def _seed_catalog():
	for row in _CATALOG:
		vals = dict(zip(_FIELDS, row))
		name = vals["field_key"]
		# section_key/target_doctype/fieldname are required by the doctype; fill sane values.
		if vals["sql_source"] in ("task", "payload"):
			vals["section_key"] = "activity"
			vals["target_doctype"] = "CRM Task"
		else:
			vals["section_key"] = "lead"
			vals["target_doctype"] = "CRM Lead"
		if frappe.db.exists("CRM Lead API Field", name):
			doc = frappe.get_doc("CRM Lead API Field", name)
			doc.update(vals)
			doc.save(ignore_permissions=True)
		else:
			frappe.get_doc(dict(doctype="CRM Lead API Field", **vals)).insert(ignore_permissions=True)


def _seed_views():
	specs = [
		dict(
			label=LEAD_VIEW, base_object="Lead", is_standard=1,
			columns=frappe.as_json(["lead:first_name", "lead:status", "lead:mobile_no"]),
			predicate=frappe.as_json({"op": "and", "conditions": []}),
		),
		dict(
			label=ACT_VIEW, base_object="Activity", activity_type=TEST_ACTIVITY_TYPE, is_standard=1,
			columns=frappe.as_json(["act:title", "act:status", "act:outcome", "act:reference", "act:scheduled", "act:cycle"]),
			predicate=frappe.as_json({"op": "and", "conditions": []}),
		),
	]
	names = {}
	for s in specs:
		existing = frappe.db.get_value("CRM Smart View", {"label": s["label"]})
		if existing:
			doc = frappe.get_doc("CRM Smart View", existing)
			doc.update(s)
			doc.save(ignore_permissions=True)
		else:
			doc = frappe.get_doc(dict(doctype="CRM Smart View", **s)).insert(ignore_permissions=True)
		names[s["base_object"]] = doc.name
	return names


def seed():
	"""Idempotent: seed the test catalog rows + the two standard test views. Returns view names."""
	_seed_catalog()
	names = _seed_views()
	frappe.db.commit()
	return names
