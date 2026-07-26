"""Give every answer a task ALREADY carries the home `field_target` names.

Phase 3 of docs/plans/task-form-layer/2026-07-25-task-slots-to-sections-and-form-layer.md. Phase 2 made every new write
land in both homes; a task saved before it answers only in a slot column or in the JSON payload, so for
the whole existing history the two homes disagree — and Phase 4, which flips the read, would find nothing
there.

End state: for every CRM Task whose type declares a schema, every declared field carrying a value in its
old home carries the same value at the address `field_target` names. Nothing is enumerated here — no
field, no slot, no section key, no mapping table. The value is read by `_legacy_task_values`, the old
writer wrote by; the row is written by `_put_section_value`, the rule the new writer writes by; a field
routed to a retained common CRM Task column (§8 rule 2) already IS in its new home and moves nothing.
Adding a home moves nothing out of one: the slots and the payload are untouched, and Phase 7 drops them.

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


def ensure_section_rows():
	"""Idempotent: re-reads the old homes and writes only the row that is missing or disagrees, so a second
	run changes nothing and an interrupted one heals. Safe to call before the sections exist."""
	if not frappe.db.table_exists("CRM Task Section"):
		return  # skip-until-ready: the sections have not synced, so no field has a new home to reach
	if not frappe.get_all("CRM Task Section", filters={"is_key_value": 1}, limit=1):
		return  # skip-until-ready: the default home is seeded in after_migrate, which is what completes this
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
		c for c in ("custom_activity_payload", *activity_api.PROMOTED_COLUMNS) if meta.has_field(c)
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
	values = activity_api._legacy_task_values(r, {"fields": [f for f, _ in routes]})
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


def reconcile():
	"""Read-only: every old-home answer against the new home it should now hold. Prints the two counts and
	names every disagreement; zero disagreements is the condition Phase 3 is done on."""
	answers = disagreed = 0
	for task_type in frappe.get_all("CRM Task Type", pluck="name"):
		routes = _routes(task_type)
		if not routes:
			continue
		for r in frappe.get_all("CRM Task", filters={"custom_task_type": task_type}, fields=_columns()):
			pending = _pending(r, routes)
			if not pending:
				continue
			answers += len(pending)
			# _put_section_value answers "does the new home already carry this?"; the doc is discarded unsaved.
			doc = frappe.get_doc("CRM Task", r.name)
			for f, _table, value in pending:
				if activity_api._put_section_value(doc, f, value):
					disagreed += 1
					print(f"  DISAGREES {r.name} {f.fieldname}")
	rows = {s.child_table_field: frappe.db.count(s.target_doctype, {"parenttype": "CRM Task"})
			for s in frappe.get_all("CRM Task Section", fields=["child_table_field", "target_doctype"])}
	print(f"old-home answers routed to a section: {answers}")
	print(f"new-home rows: {rows}")
	print(f"DISCREPANCY: {disagreed}")
	frappe.db.rollback()
