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
otherwise get the doctype with no index at all. **That membership is also why this module is edited in
place rather than superseded by a new patch: `_STEPS` re-runs `execute()` on every migrate, so a fix here
reaches every site on its next one. The "an applied patch is dead" rule is about `patches.txt` semantics,
and does not apply to a module the schema pass drives.**

The unique index went in as a hand-written `ALTER TABLE` wrapped in a bare `except`, which is two defects
in one line: raw SQL (banned outright), and a failure that leaves the migrate GREEN with no uniqueness.
`timeline._write` is built entirely on that constraint — it does not pre-check, it inserts and lets the
database reject the duplicate — so without it every double-fired hook writes a second pointer and the
rail shows the same call or note twice, with `has_index` making sure no later migrate ever repairs it.
`frappe.db.add_unique` is the native door and two patches already use it for this exact shape.

A unique index cannot be added over existing duplicates, so the same shape `add_lead_dedup_unique_index`
already uses applies here: look first, and if duplicates exist say so loudly and skip, rather than
creating the index or quietly deleting rows inside a patch.
"""
import frappe

# `table_exists`, `add_index` and `add_unique` take the DOCTYPE (they prefix `tab` themselves);
# `has_index` takes the real table name. Mixing the two silently no-ops — the guard just returns early.
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
		if unique and _has_duplicates(fields):
			frappe.log_error(
				title=f"Timeline index {name} skipped",
				message=(
					f"Duplicate {tuple(fields)} rows exist on {DOCTYPE}, so the unique index was NOT created "
					"and timeline.index_event's no-op-on-redelivery guarantee is unenforced at the database. "
					"Run timeline.rebuild() for the affected leads, then re-run this step."
				),
			)
			continue
		try:
			frappe.db.add_unique(DOCTYPE, fields, name) if unique else frappe.db.add_index(DOCTYPE, fields, name)
		except Exception:
			frappe.log_error(title=f"Timeline index {name} failed", message=frappe.get_traceback())


def _has_duplicates(fields):
	"""True if any (source_doctype, source_name) is held by more than one pointer. Grouped in SQL by
	frappe's own query builder — the biggest group is all we need, so no HAVING and no second pass."""
	worst = frappe.get_all(
		DOCTYPE, fields=[*fields, "count(name) as held"],
		group_by=", ".join(fields), order_by="held desc", limit=1,
	)
	return bool(worst) and worst[0].held > 1
