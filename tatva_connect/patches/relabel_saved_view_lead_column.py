# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""The lead column reads `Lead` for the reps who already customised their list.

`default_list_data()` seeds a view; it does not own one afterwards. The moment a rep changes a column or a
sort, `create_or_update_standard_view` writes a `CRM View Settings` row and the server returns THAT row's
columns from then on (`crm/api/doc.py:309,331`). So renaming the label in code reaches everyone who never
touched their list and nobody who did — which is the half of the population most likely to be looking.

This is the one-shot repair. For a STANDARD row on the three child listing doctypes it rewrites the label
of the column whose key is `reference_docname`, and nothing else: not the width the rep dragged, not the
order they chose, not a column they added, and never a row somebody built for another purpose. It creates
no row — views are 100% user-built (invariant A.17).

Declares an end state and assumes nothing about what ran before: it reads each row's real columns, writes
only where the label is not already `Lead`, and is a free no-op on a site with no such rows and on every
run after the first.

Plan: docs/plans/list-view-cleanup/2026-07-31-listing-lead-column-and-row-click.md §5, phase A4.
"""
import frappe

VIEW_SETTINGS = "CRM View Settings"

# The three child listing surfaces whose lead reference is a Dynamic Link; the key and the label are the
# declaration's, and `tests/architecture/test_listing_declaration_home.py` is what keeps the two agreeing.
DOCTYPES = ("CRM Task", "CRM Call Log", "FCRM Note")
LEAD_COLUMN_KEY = "reference_docname"
LEAD_COLUMN_LABEL = "Lead"


def execute():
	rows = frappe.get_all(
		VIEW_SETTINGS, filters={"dt": ["in", DOCTYPES], "is_standard": 1}, fields=["name", "columns"]
	)
	for row in rows:
		columns = frappe.parse_json(row.columns or "[]")
		if not isinstance(columns, list):
			continue
		changed = False
		for column in columns:
			if not isinstance(column, dict):
				continue
			if column.get("key") == LEAD_COLUMN_KEY and column.get("label") != LEAD_COLUMN_LABEL:
				column["label"] = LEAD_COLUMN_LABEL
				changed = True
		if not changed:
			continue
		frappe.db.set_value(
			VIEW_SETTINGS, row.name, "columns", frappe.as_json(columns), update_modified=False
		)  # authz-ok: tier-c — migrate/patch, no session user
	frappe.db.commit()
