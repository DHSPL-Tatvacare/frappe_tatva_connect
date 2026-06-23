"""CHILD-TABLE FLATTENING PROOF — run on a dev bench with:

    bench --site dev.localhost execute tatva_connect.smartview.tests.run_child_flatten_proof.run

Proves the composer linearises a CRM Lead parent + a child-table row into ONE flat row:
  * a catalog field with sql_source='child' (target_doctype = the child doctype) is LEFT-JOINed
    on parent=name AND parenttype, and projected as the field_key,
  * the lead appears exactly ONCE (no row-per-child duplication) for a single-row child,
  * the child value lands under the field_key alongside parent columns,
  * it all rides the one catalog/resolve_fields brain (get_data path).

Self-seeds: picks the first CRM Lead child table it can write, makes a lead with one child row
carrying a sentinel, seeds the two catalog rows + a view, asserts. Idempotent.
"""
import frappe

from tatva_connect.smartview import api

TAG = "ZCHILD_PROOF"
SENTINEL = "CHILDVAL-42"
PARENT_KEY = "lead:first_name"   # parent column (also the search handle)
CHILD_KEY = "zchild:val"         # the child column under test
VIEW_LABEL = "ZCHILD Smart View"


def _dummy(cf):
	ft = cf.fieldtype
	if ft in ("Int", "Float", "Currency", "Percent"):
		return 1
	if ft == "Date":
		return "2026-01-01"
	if ft == "Datetime":
		return "2026-01-01 00:00:00"
	if ft == "Select":
		return (cf.options or "").strip().split("\n")[0] or "x"
	return "ZC"


def _pick_writable_child():
	"""First CRM Lead child table with a Data/text field we can project, whose required fields are
	all fillable without a Link lookup. Returns (parent_table_field, child_doctype, proj_field, reqd)."""
	for f in frappe.get_meta("CRM Lead").fields:
		if f.fieldtype != "Table":
			continue
		cm = frappe.get_meta(f.options)
		text = [cf for cf in cm.fields if cf.fieldtype in ("Data", "Small Text", "Text")]
		reqd_links = [cf for cf in cm.fields if cf.reqd and cf.fieldtype in ("Link", "Dynamic Link")]
		if text and not reqd_links:
			proj = text[0].fieldname
			reqd = {cf.fieldname: _dummy(cf) for cf in cm.fields if cf.reqd and cf.fieldname != proj}
			return f.fieldname, f.options, proj, reqd
	raise RuntimeError("No writable CRM Lead child table found for the flatten proof.")


def _seed_catalog(child_dt, child_field):
	rows = [
		dict(field_key=PARENT_KEY, label="First Name", fieldname="first_name", section_key="lead",
			 target_doctype="CRM Lead", sql_source="parent", applies_to="lead",
			 filterable=1, sortable=1, surface="worklist"),
		dict(field_key=CHILD_KEY, label="Child Val", fieldname=child_field, section_key="child",
			 target_doctype=child_dt, sql_source="child", child_pick="single", applies_to="lead",
			 filterable=1, sortable=1, surface="worklist"),
	]
	for r in rows:
		if frappe.db.exists("CRM Lead API Field", r["field_key"]):
			frappe.get_doc("CRM Lead API Field", r["field_key"]).update(r).save(ignore_permissions=True)
		else:
			frappe.get_doc(dict(doctype="CRM Lead API Field", **r)).insert(ignore_permissions=True)


def _seed_lead(parent_table_field, proj_field, reqd):
	for ld in frappe.get_all("CRM Lead", filters={"lead_name": ["like", "%" + TAG + "%"]}, pluck="name"):
		frappe.delete_doc("CRM Lead", ld, force=True, ignore_permissions=True)
	lead = frappe.get_doc({"doctype": "CRM Lead", "first_name": TAG, "lead_name": TAG, "status": "New"})
	row = dict(reqd)
	row[proj_field] = SENTINEL
	lead.append(parent_table_field, row)
	lead.insert(ignore_permissions=True)
	return lead.name


def _seed_view():
	existing = frappe.db.get_value("CRM Smart View", {"label": VIEW_LABEL})
	spec = dict(label=VIEW_LABEL, base_object="Lead", is_standard=1,
				columns=frappe.as_json([PARENT_KEY, CHILD_KEY]),
				predicate=frappe.as_json({"op": "and", "conditions": []}))
	if existing:
		frappe.get_doc("CRM Smart View", existing).update(spec).save(ignore_permissions=True)
		return existing
	return frappe.get_doc(dict(doctype="CRM Smart View", **spec)).insert(ignore_permissions=True).name


def _check(label, cond):
	print("  [{0}] {1}".format("PASS" if cond else "FAIL", label))
	return cond


def run():
	frappe.flags.in_test = True
	parent_table_field, child_dt, proj_field, reqd = _pick_writable_child()
	print("    child doctype:", child_dt, "| field:", proj_field, "| via:", parent_table_field)
	_seed_catalog(child_dt, proj_field)
	lead = _seed_lead(parent_table_field, proj_field, reqd)
	view = _seed_view()
	frappe.db.commit()

	r = []
	data = api.get_data(view, search=TAG)
	cols = [c["key"] for c in data["columns"]]
	print("    columns:", cols, "| total:", data["total"])
	r.append(_check("child column projected in column set", CHILD_KEY in cols))
	r.append(_check("parent + child both projected", PARENT_KEY in cols and CHILD_KEY in cols))
	r.append(_check("lead appears exactly ONCE (no row-per-child dup)", data["total"] == 1 and len(data["rows"]) == 1))
	row0 = data["rows"][0] if data["rows"] else {}
	print("    row:", {k: row0.get(k) for k in (PARENT_KEY, CHILD_KEY, "name")})
	r.append(_check("child value linearised onto the parent row", row0.get(CHILD_KEY) == SENTINEL))
	r.append(_check("parent value present on the same row", (row0.get(PARENT_KEY) or "") == TAG))

	ok = all(r)
	print("\n==== CHILD-FLATTEN PROOF {0} ({1}/{2} checks passed) ====".format(
		"PASSED" if ok else "FAILED", sum(1 for x in r if x), len(r)))
	return ok
