"""(owner, modified) and (assigned_to, modified) on CRM Task — the two branches of the row-visibility
predicate that carried no index at all.

WHAT IT COSTS TODAY. `access/visibility.py:_compose` ORs a doctype's strategies, so every non-privileged
list read of CRM Task narrows on `owner = me OR assigned_to = me OR (reference_doctype = 'CRM Lead' AND
reference_docname IN (<visible leads>))`. Measured on prod 2026-10-01: 976,971 rows examined to return
ZERO rows, 10.5 s; the `total_count` twin examined the same to return 1. `reference_docname` leads two
existing composites, but neither `owner` nor `assigned_to` had any index whatsoever, so two of the three
branches had nothing to seek on and the optimiser discarded the third with them. One list open pays that
scan twice, and a COUNT can never stop early the way the LIMITed fetch can.

WHY COMPOSITE AND NOT `search_index`. `database/schema.py:311` drops any index on a column whose meta does
not declare `search_index`, and it finds that index BY COLUMN rather than by name — so a single-column
`ix_task_owner` would be deleted by the next schema sync. Composite indexes are exempt, which is why every
index patch in this app is composite. `owner` is a frappe standard column with no DocField to hang the
flag on, so the Property Setter route (add_lead_read_path_indexes) is not available to it either.

WHY `modified` IS THE SECOND COLUMN. It is the list's own sort, so the pair also spares the filesort the
seek would otherwise feed, and it keeps the index useful to any "my tasks, newest first" read.

Selectivity: ~400 distinct users over ~604k rows is ~0.25% per user — five to eight times narrower than
`custom_task_type` (49 distinct), which this table already indexes and relies on. Skew is the open
question: a migration account holding a large share of `owner` makes that one branch wide, which is worth
measuring but does not make the index wrong for everyone else.

The ALTER is ALGORITHM=INPLACE, LOCK=NONE for a secondary index, so reads and writes continue while it
builds. Idempotent (has_index guard). Also in schema_setup._STEPS, because install-app baselines
patches.txt without running it.
"""

import frappe

_DOCTYPE = "CRM Task"
_TABLE = "tabCRM Task"
_INDEXES = (
	("ix_task_owner_modified", ("owner", "modified")),
	("ix_task_assigned_modified", ("assigned_to", "modified")),
)


def execute():
	if not frappe.db.table_exists(_DOCTYPE):
		return
	for name, columns in _INDEXES:
		if frappe.db.has_index(_TABLE, name):
			continue
		try:
			frappe.db.add_index(_DOCTYPE, list(columns), name)
		except Exception:
			frappe.log_error(frappe.get_traceback(), f"tasks: index {name} failed")
