"""TEST-ONLY seed — two standard Smart Views + their catalog rows, for the P0 headless proof.

This is NOT business data and NOT a fixture: it ships under tests/, is idempotent, and is only
ever run by the proof harness (run_proof) on a dev bench. Business catalog rows + real views ship
as db-seeds the operator runs by hand (CLAUDE.md A5).

Only LEAD fields are seeded here. An activity's fields are not catalogued in this table and cannot be:
they live on the task type that declares them, and the composer asks the activity brain for them. The
Activity view below therefore names a task type and seeds no columns of its own — its field set IS that
type's schema.
"""
import frappe

# (field_key, label, fieldname, section, filterable, sortable, surface) — all CRM Lead parent fields.
_CATALOG = [
	("lead:first_name", "First Name", "first_name", "lead", 1, 1, "worklist"),
	("lead:status", "Status", "status", "lead", 1, 1, "worklist"),
	("lead:mobile_no", "Mobile", "mobile_no", "lead", 1, 0, "worklist"),
]

_FIELDS = ["field_key", "label", "fieldname", "section", "filterable", "sortable", "surface"]

TEST_ACTIVITY_TYPE = "Order Punch Status"
LEAD_VIEW = "TEST Smart View — Leads"
ACT_VIEW = "TEST Smart View — Order Punch"


def _seed_catalog():
	for row in _CATALOG:
		vals = dict(zip(_FIELDS, row, strict=False))
		name = vals["field_key"]
		# section_key mirrors section until its last reader moves; target_doctype is reqd on the doctype.
		vals["section_key"] = vals["section"]
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
			# No columns: an empty set falls back to every worklist field the type's schema declares.
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
