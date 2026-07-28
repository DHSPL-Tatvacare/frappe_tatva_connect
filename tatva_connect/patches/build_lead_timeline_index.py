"""Index CRM Timeline Event — the structures its doctype JSON cannot declare.

The Activity rail reads this ONE table instead of merging six at read time (see activity/timeline.py for
why). Two structures the doctype JSON cannot declare, both composite:

  * (reference_doctype, reference_name, event_on) — the rail's whole query. The two equality columns
    lead, so the same index serves the ORDER BY and the LIMIT.
  * UNIQUE (source_doctype, source_name) — one pointer per source row. This is what makes THIS patch
    re-runnable, a double-fired hook a no-op, and two concurrent writers safe.

STRUCTURE ONLY — this patch does NOT fill the table. The index is a dormant operator toggle
(`Activity::Timeline::indexing`), so the fill is its activator's job and runs when the operator switches
it on, in the background. A patch that backfilled here would populate a table nothing reads yet, on every
site, whether or not the operator ever enables it.

Also in schema_setup._STEPS — install-app baselines this line without running it, so a fresh site would
otherwise get the doctype with no index at all.
"""
import frappe

# `table_exists` and `add_index` take the DOCTYPE (they prefix `tab` themselves); `has_index` and a raw
# ALTER take the real table name. Mixing the two silently no-ops — the guard just returns early.
DOCTYPE = "CRM Timeline Event"
TABLE = f"tab{DOCTYPE}"
INDEXES = (
	("ix_timeline_ref_event", ["reference_doctype", "reference_name", "event_on"], False),
	("ix_timeline_source_unique", ["source_doctype", "source_name"], True),
)


def execute():
	if not frappe.db.table_exists(DOCTYPE):
		return
	for name, fields, unique in INDEXES:
		if frappe.db.has_index(TABLE, name):
			continue
		try:
			if unique:
				cols = ", ".join(f"`{f}`" for f in fields)
				frappe.db.sql(f"ALTER TABLE `{TABLE}` ADD UNIQUE INDEX `{name}` ({cols})")
			else:
				frappe.db.add_index(DOCTYPE, fields, name)
		except Exception:
			frappe.log_error(title=f"Timeline index {name} failed", message=frappe.get_traceback())
