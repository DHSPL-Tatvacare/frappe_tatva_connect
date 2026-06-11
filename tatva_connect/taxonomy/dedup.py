"""Near-duplicate detection + safe merge for entity masters (Doc B Phase 3 / M-2 cleanup).

Entity masters (Doctor, Hospital, City) are hash/composite keyed now, so two rows
that normalize to the same display value can coexist — that's the point (real same-
named entities), but it also lets accidental dupes slip in. This module surfaces
normalized-value collisions and offers a safe merge.

Merge is safe because references are opaque ids: frappe.rename_doc(merge=True) folds
the source row into the target and re-points every inbound link, then drops the source.
"""
import frappe
from frappe import _

from tatva_connect.taxonomy.normalize import normalize_display

# The display field per entity master.
_DISPLAY = {
	"CRM Doctor": "doctor_name",
	"CRM Hospital": "hospital_name",
	"CRM City": "city_name",
}


@frappe.whitelist()
def near_duplicates(doctype):
	"""Return groups of master rows that collapse to the same NORMALIZED display value.

	Each group is {"value": <normalized>, "rows": [{"name","display"}...]} with 2+ rows.
	Only System Manager (the master-admin) may run it.
	"""
	frappe.only_for("System Manager")
	display_field = _DISPLAY.get(doctype)
	if not display_field:
		frappe.throw(_("{0} is not a mergeable entity master.").format(doctype))

	buckets = {}
	for r in frappe.get_all(doctype, fields=["name", display_field]):
		val = normalize_display(r.get(display_field) or "")
		if not val:
			continue
		buckets.setdefault(val.lower(), []).append({"name": r.name, "display": r.get(display_field)})

	return [
		{"value": rows[0]["display"], "rows": rows}
		for rows in buckets.values()
		if len(rows) > 1
	]


@frappe.whitelist()
def merge_masters(doctype, source, target):
	"""Merge `source` into `target` (re-points all inbound links, drops source).

	Safe because the master `name` is an opaque id — references move transparently.
	"""
	frappe.only_for("System Manager")
	if doctype not in _DISPLAY:
		frappe.throw(_("{0} is not a mergeable entity master.").format(doctype))
	if source == target:
		frappe.throw(_("Source and target are the same record."))
	if not (frappe.db.exists(doctype, source) and frappe.db.exists(doctype, target)):
		frappe.throw(_("Both source and target must exist."))
	frappe.rename_doc(doctype, source, target, merge=True)
	frappe.db.commit()
	return {"merged_into": target}
