"""Give every answer a task ALREADY carries the home `field_target` names.

Phase 3 of docs/plans/task-form-layer/2026-07-25-task-slots-to-sections-and-form-layer.md. Phase 2 made every new write
land in both homes; a task saved before it answers only in a slot column or in the JSON payload, so for
the whole existing history the two homes disagree — and Phase 4, which flips the read, would find nothing
there.

End state: for every CRM Task whose type declares a schema, every declared field carrying a value in its
old home carries the same value at the address `field_target` names. Nothing is enumerated here — no
field, no section key, no mapping table. The value is read by `_legacy_task_values`, the rule the old
writer wrote by; the row is written by `_put_section_value`, the rule the new writer writes by; a field
routed to a retained common CRM Task column (§8 rule 2) already IS in its new home and moves nothing.

This module is also the HOME OF THE HISTORY. `PROMOTED_COLUMNS`, `field_column` and
`_legacy_task_values` were the live brain's until Phase 7 dropped the columns they read: they name a
schema this app no longer has, and §11 says a migration is the one place allowed to know that. Nothing
outside this module and its patch reads them, and `activity/api.py` keeps only the live seam
(`COMMON_COLUMNS`, `field_target`).

Why this lives here and not only in its patch: the answers land in Table fields that ship in
`fixtures/custom_field.json`, and the sections that route them are seeded in `after_migrate` — both AFTER
post-model-sync patches (frappe/migrate.py:139-202). On the upgrade that carries Phases 1-3 in one
migrate the patch therefore runs before its own prerequisites exist, no-ops, and is logged applied — dead
for ever. So the patch is skip-until-ready and this same function runs in `after_migrate`, which is the
pass that completes it. Exactly the contract `partner_api.section_seed.ensure_rows` already ships under.
"""
import frappe

from tatva_connect.activity import api as activity_api

# Tasks per commit. Measured, not guessed: the UAT replay carries 12,562 activity tasks holding 62,957
# payload answers, so one transaction for the lot is a lock held for minutes — and a commit per chunk is
# the whole of the resumability this needs, because a re-run over the finished part writes nothing.
_CHUNK = 500

# The 9 CRM Task columns an activity field's answer could be PROMOTED into before Phase 5, and the JSON
# payload that held everything else. HISTORY, not a live rule: `patches/retire_task_slot_columns.py` drops
# the five the task row does not keep plus the payload, and `activity_api.COMMON_COLUMNS` is what survives.
PROMOTED_COLUMNS = (
	"custom_outcome", "custom_reference", "custom_asm",
	"custom_scheduled_at", "custom_followup_at",
	"custom_key_date_1", "custom_key_date_2", "custom_key_date_3", "custom_key_date_4",
)
_PAYLOAD_COLUMN = "custom_activity_payload"


def field_column(f):
	"""The promoted CRM Task column a schema field's answer lived in before Phase 5, or None when it lived
	in the JSON payload. The HISTORY reader's rule and only its: no writer and no live reader asks it."""
	target = f.get("target") or ""
	return target if target in PROMOTED_COLUMNS else None


def _legacy_task_values(r, cfg):
	"""The OLD homes — the JSON payload merged with the promoted columns, routed by `field_column`.

	The backfill's reader, and only the backfill's: a migration reads where a value IS today, which is the
	whole of what it has to move. Answers nothing once Phase 7 has dropped the columns it reads."""
	vals = {}
	if (r.get(_PAYLOAD_COLUMN) or "").strip():
		try:
			vals.update(frappe.parse_json(r.get(_PAYLOAD_COLUMN)) or {})
		except Exception:
			frappe.log_error(f"activity: bad payload on task {r.name}")
	if cfg:
		for f in cfg["fields"]:
			tgt = field_column(f)
			if tgt and r.get(tgt) not in (None, ""):
				vals[f["fieldname"]] = str(r.get(tgt))
	if r.description:
		vals.setdefault("notes", r.description)
	return vals


def retired_homes():
	"""The old homes Phase 7 removes: the JSON payload, plus every promoted column the task row does NOT
	keep. Derived from the two lists that already exist, so the drop list is stated once — in the patch."""
	return (_PAYLOAD_COLUMN, *(c for c in PROMOTED_COLUMNS if c not in activity_api.COMMON_COLUMNS))


def _readable_old_homes():
	"""The retired old homes this site still physically has. Empty means the copy is finished and dropped:
	there is no column left holding an answer, so there is nothing left for this module to move."""
	meta = frappe.get_meta("CRM Task")
	return [c for c in retired_homes() if meta.has_field(c)]


def ensure_section_rows():
	"""Idempotent: re-reads the old homes and writes only the row that is missing or disagrees, so a second
	run changes nothing and an interrupted one heals. Safe to call before the sections exist."""
	if not frappe.db.table_exists("CRM Task Section"):
		return  # skip-until-ready: the sections have not synced, so no field has a new home to reach
	if not frappe.get_all("CRM Task Section", filters={"is_key_value": 1}, limit=1):
		return  # skip-until-ready: the default home is seeded in after_migrate, which is what completes this
	if not _readable_old_homes():
		return  # done-and-dropped: Phase 7 removed every old home, so no answer is anywhere else any more
	done = 0
	for task_type in frappe.get_all("CRM Task Type", pluck="name"):
		routes = _routes(task_type)
		if not routes:
			continue  # a type declaring no schema has no answer to move, and no address to move it to
		for r in frappe.get_all("CRM Task", filters={"custom_task_type": task_type}, fields=_columns()):
			_backfill_one(r, routes)
			done += 1
			if done % _CHUNK == 0:
				frappe.db.commit()


def _columns():
	"""What an old home can be read from: the payload, the promoted columns, and the notes the reader merges.
	Filtered through meta so this still runs once Phase 7 has dropped the columns it reads today."""
	meta = frappe.get_meta("CRM Task")
	return ["name", "description"] + [
		c for c in (_PAYLOAD_COLUMN, *PROMOTED_COLUMNS) if meta.has_field(c)
	]


def _routes(task_type):
	"""Every declared field of the type paired with the section child table its answer lands in — resolved
	ONCE per type by the writer's own router, because where a field lives is a fact about the declaration
	and never about the task. A None table is §8 rule 2: the task row already IS the new home."""
	out = []
	for f in frappe.get_cached_doc("CRM Task Type", task_type).schema:
		f = frappe._dict(f.as_dict())
		section_key = activity_api.field_target(f)[0]
		table = frappe.get_cached_value("CRM Task Section", section_key, "child_table_field") if section_key else None
		out.append((f, table))
	return out


def _pending(r, routes):
	"""The (field, table, value) triples this task has an answer for, read by the rule the old writer wrote
	by — so a column the writer never filled can never be mistaken for an answer. It reads the OLD homes
	explicitly: Phase 4 moved `_task_values` onto the section rows, which is the very thing being filled."""
	values = _legacy_task_values(r, {"fields": [f for f, _ in routes]})
	return [(f, table, values[f.fieldname]) for f, table in routes
			if table and values.get(f.fieldname) not in (None, "")]


def _backfill_one(r, routes):
	"""One task: put each old answer at its new address and persist ONLY the child tables that changed. The
	upsert is the dual writer's own, so a row that already agrees is left exactly as it is."""
	pending = _pending(r, routes)
	if not pending:
		return
	doc = frappe.get_doc("CRM Task", r.name)
	touched = {table for f, table, value in pending if activity_api._put_section_value(doc, f, value)}
	for table in touched:
		doc.update_child_table(table)


def audit(limit=0):
	"""(routed, unhomed) — how many answers still sitting in an old home route to a section, and which of
	them the new home does not carry yet.

	THE one question "has the copy finished?", asked by `reconcile` for a report and by
	`patches/retire_task_slot_columns.py` before it removes the old home — one predicate, so a report and a
	refusal can never disagree. `limit` stops at the first N unhomed, which is all a refusal needs to know.
	Writes nothing: `_put_section_value` only mutates the loaded doc, which is then discarded."""
	routed, unhomed = 0, []
	if not _readable_old_homes():
		return routed, unhomed  # every old home is already dropped, so nothing can be sitting in one
	for task_type in frappe.get_all("CRM Task Type", pluck="name"):
		routes = _routes(task_type)
		if not routes:
			continue
		for r in frappe.get_all("CRM Task", filters={"custom_task_type": task_type}, fields=_columns()):
			pending = _pending(r, routes)
			if not pending:
				continue
			routed += len(pending)
			doc = frappe.get_doc("CRM Task", r.name)
			for f, _table, value in pending:
				if activity_api._put_section_value(doc, f, value):
					unhomed.append((r.name, f.fieldname))
					if limit and len(unhomed) >= limit:
						return routed, unhomed
	return routed, unhomed


def reconcile():
	"""Read-only: every old-home answer against the new home it should now hold. Prints the two counts and
	names every disagreement; zero disagreements is the condition Phase 3 is done on."""
	routed, unhomed = audit()
	for task, fieldname in unhomed:
		print(f"  DISAGREES {task} {fieldname}")
	rows = {s.child_table_field: frappe.db.count(s.target_doctype, {"parenttype": "CRM Task"})
			for s in frappe.get_all("CRM Task Section", fields=["child_table_field", "target_doctype"])}
	print(f"old-home answers routed to a section: {routed}")
	print(f"new-home rows: {rows}")
	print(f"DISCREPANCY: {len(unhomed)}")
	frappe.db.rollback()
