"""MULTI-ROW CHILD-FLATTEN PROOF — run on a dev bench with:

    bench --site dev.localhost execute tatva_connect.smartview.tests.run_child_flatten_proof.run

Proves the composer linearises a CRM Lead parent + its multi-row `lab` child into ONE flat row, picking
the LATEST child by date — the locked "latest wins" rule (ADR: a multi-row section shows the latest row
across every consumer). It rides the REAL `lab` section, so `sql_source`/`row_key_field` come from the one
`CRM Lead Section` brain, not from anything this script writes:

  * a lead with TWO `custom_lab_profile` rows (different `report_date`) appears exactly ONCE,
  * the projected `lab:hba1c` is the value from the row with the NEWEST `report_date`,
  * it all rides the one catalog/resolve_fields brain (get_data path).

Self-seeds a lead + two lab rows + a view, asserts, idempotent. Seeds NO catalog rows: `lab:hba1c` and
`lead:first_name` are the real, already-seeded catalog — the point is that this proof invents no fork.
"""
import frappe

from tatva_connect.smartview import api

TAG = "ZLAB_PROOF"
LEAD_KEY = "lead:first_name"           # real catalog handle: worklist, searchable
LAB_KEY = "lab:hba1c"                  # real multi-row lab column, picked latest_by report_date
OLD_DATE, OLD_VAL = "2026-01-01", 8.1
NEW_DATE, NEW_VAL = "2026-07-01", 6.9  # the newest report_date -> this hba1c must win
VIEW_LABEL = "ZLAB Latest Smart View"


def _seed_lead():
	"""A lead with TWO lab rows so latest_by must pick exactly one (the newer report's value)."""
	for ld in frappe.get_all("CRM Lead", filters={"lead_name": ["like", "%" + TAG + "%"]}, pluck="name"):
		frappe.delete_doc("CRM Lead", ld, force=True, ignore_permissions=True)
	lead = frappe.get_doc({"doctype": "CRM Lead", "first_name": TAG, "lead_name": TAG, "status": "New"})
	for report_date, hba1c in ((OLD_DATE, OLD_VAL), (NEW_DATE, NEW_VAL)):
		lead.append("custom_lab_profile", {"report_date": report_date, "hba1c": hba1c})
	lead.insert(ignore_permissions=True)
	return lead.name


def _seed_view(label, columns):
	existing = frappe.db.get_value("CRM Smart View", {"label": label})
	spec = dict(label=label, base_object="Lead", is_standard=1,
				columns=frappe.as_json(columns),
				predicate=frappe.as_json({"op": "and", "conditions": []}))
	if existing:
		frappe.get_doc("CRM Smart View", existing).update(spec).save(ignore_permissions=True)
		return existing
	return frappe.get_doc(dict(doctype="CRM Smart View", **spec)).insert(ignore_permissions=True).name


def _check(label, cond):
	print("  [{}] {}".format("PASS" if cond else "FAIL", label))
	return cond


def _as_float(v):
	"""The projected value, coerced for comparison; None/blank -> None so a miss FAILs, never throws."""
	try:
		return float(v) if v not in (None, "") else None
	except (TypeError, ValueError):
		return None


def run():
	frappe.flags.in_test = True
	_seed_lead()
	view = _seed_view(VIEW_LABEL, [LEAD_KEY, LAB_KEY])
	frappe.db.commit()

	r = []
	print("\n== multi-row lab flatten (2 lab rows -> 1 lead row, newest report_date wins) ==")
	data = api.get_data(view, search=TAG)
	cols = [c["key"] for c in data["columns"]]
	print("    columns:", cols, "| total:", data["total"])
	r.append(_check("lab column projected in column set", LAB_KEY in cols))
	r.append(_check("parent + lab both projected", LEAD_KEY in cols and LAB_KEY in cols))
	r.append(_check("lead with 2 lab rows appears exactly ONCE (no row-per-child dup)",
					data["total"] == 1 and len(data["rows"]) == 1))
	row0 = data["rows"][0] if data["rows"] else {}
	print("    row:", {k: row0.get(k) for k in (LEAD_KEY, LAB_KEY, "name")})
	r.append(_check("latest_by report_date picked the NEWEST hba1c (Jul 6.9 over Jan 8.1)",
					_as_float(row0.get(LAB_KEY)) == NEW_VAL))
	r.append(_check("parent value present on the same row", (row0.get(LEAD_KEY) or "") == TAG))

	ok = all(r)
	print("\n==== CHILD-FLATTEN PROOF {} ({}/{} checks passed) ====".format(
		"PASSED" if ok else "FAILED", sum(1 for x in r if x), len(r)))
	return ok
