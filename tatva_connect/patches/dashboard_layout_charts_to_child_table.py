"""A dashboard layout's placements move from a JSON column into child rows.

`CRM Dashboard Layout.layout` held `[{"chart","x","y","w","h"}, ...]` as text, so a pointer at a card that
did not exist was only caught by hand-written validation and a wrong-shaped entry only at render. They are
now `CRM Dashboard Layout Chart` rows whose `chart` is a Link, which frappe refuses on its own.

Reads the old column and writes the rows, once, for any layout that has none yet. Idempotent: a layout that
already has child rows is left alone, so a re-run cannot duplicate a placement. The old column is left in
place — frappe does not drop removed fields, and a column nobody reads is cheaper than a DDL that cannot be
undone if this needs re-running.

No schema_setup twin: a fresh site has no old layouts to convert, and the seed writes child rows directly.
"""

import frappe

LAYOUT = "CRM Dashboard Layout"
PLACEMENT = "CRM Dashboard Layout Chart"
_KEYS = ("chart", "x", "y", "w", "h")


def execute():
	if not (frappe.db.table_exists(LAYOUT) and frappe.db.table_exists(PLACEMENT)):
		return
	if not frappe.db.has_column(LAYOUT, "layout"):
		return

	for name in frappe.get_all(LAYOUT, pluck="name"):  # authz-ok: tier-a — migration, no session user
		if frappe.db.exists(PLACEMENT, {"parent": name, "parenttype": LAYOUT}):
			continue
		stored = frappe.db.get_value(LAYOUT, name, "layout")
		try:
			placements = frappe.parse_json(stored or "[]")
		except (TypeError, ValueError):
			continue
		if not isinstance(placements, list):
			continue

		doc = frappe.get_doc(LAYOUT, name)
		for placement in placements:
			if not isinstance(placement, dict) or not placement.get("chart"):
				continue
			# A card the catalogue no longer has would fail the Link; drop it rather than fail the migrate.
			if not frappe.db.exists("CRM Dashboard Chart", placement["chart"]):
				continue
			doc.append("charts", {key: placement.get(key) for key in _KEYS})
		if doc.charts:
			doc.save(ignore_permissions=True)  # authz-ok: tier-a — migration, no session user

	frappe.db.commit()
