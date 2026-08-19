"""The Tasks-by-Owner-and-Status heatmap leaves every dashboard.

MEASURED ON PROD, for a Sales Manager, 2026-08-20. The dashboard's twelve cards run one after another in
one request and cost 23,389 ms; this card alone was 10,483 ms of it — 45%, more than every lead card put
together (the six lead cards total 513 ms). It groups 646,027 tasks by assignee and status with no date
bound at all, and a `CRM Task` row gate is `reference_docname in (every lead you can see)`, so the whole
table is walked through a disjunction of subqueries to draw one tile.

It is also the wrong question for a dashboard. "Who is carrying what right now, across all time" is a
report someone reads once a week, not a tile every manager pays for on every page load.

WHY A PATCH AND NOT JUST THE SEED. `LAYOUT_STRUCTURAL` is `(priority, exposed_filters)`, so `ensure_rows`
re-asserts those two fields and NOTHING else — a layout's `charts` are written once, when the row is first
created. A site that already holds its layouts therefore keeps whatever it was first seeded with, and
there is no fresh site any more. Editing `seed._PLACED` alone is invisible on every existing site; the
rows have to be taken out here.

The chart row is DISABLED rather than deleted. `declaration.charts` reads `enabled=1`, so disabling drops
the card out of any layout that names it, and unlike a delete it cannot fail part-way through a migrate on
a linked row. Deleting the row as well is a one-liner whenever the config table is being tidied.
"""

import frappe

from tatva_connect.dashboard import declaration

CHART = "tasks_by_owner_and_status"


def execute():
	if not (frappe.db.table_exists(declaration.CHART) and frappe.db.table_exists(declaration.PLACEMENT_DOCTYPE)):
		return

	# Close the gap the removal leaves: anything below this card moves up by exactly its height, whatever
	# the operator arranged around it. Read before the delete, because the row is what says where it was.
	for placement in frappe.get_all(
		declaration.PLACEMENT_DOCTYPE, filters={"chart": CHART}, fields=["name", "parent", "y", "h"]
	):
		frappe.db.sql(
			f"""update `tab{declaration.PLACEMENT_DOCTYPE}` set y = y - %s
			     where parent = %s and parenttype = %s and y > %s""",
			(placement.h, placement.parent, declaration.LAYOUT, placement.y),
		)
		frappe.db.delete(declaration.PLACEMENT_DOCTYPE, {"name": placement.name})

	if frappe.db.exists(declaration.CHART, CHART):
		frappe.db.set_value(declaration.CHART, CHART, "enabled", 0)

	frappe.db.commit()
