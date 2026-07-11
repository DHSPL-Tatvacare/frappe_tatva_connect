"""Immutable rule definitions — the ONE brain over `CRM Automation Rule Version` (A.8).

WHY THIS EXISTS
---------------
A rule is a PROGRAM: an ordered list of actions. A lead entering it starts an EXECUTION. A `Wait`
action parks that execution for days or months. Before this module, the parked row held an integer
POSITION and re-read the action list live out of the mutable rule at resume time — so reordering a
rule could re-send a WhatsApp template to a patient, deleting an action could shift every parked lead
by one, and deleting the rule orphaned them all.

The defect was never the integer. It was that **a running execution depended on a mutable definition.**

So: on save, the rule's decision-relevant payload is frozen into a content-addressed, immutable
version, and every execution binds to a VERSION, never to the rule. Freeze the list and the integer
cursor is simply correct.

WHAT IS FROZEN
--------------
`on_doctype`, `event`, the grain axes, the criteria, and EVERY action (both lanes) in order — each
action carrying its child-row `name`. That name is the identity the migration prefix check compares;
without it this design collapses. `enabled` / `priority` / `description` are NOT the program and are
deliberately excluded, so toggling a rule mints nothing.

Lane is a property of CODE (`actions._ACTION_LANES`), not of data, so both lanes are frozen and the
executor filters at run time.

MIGRATION — "my fix must reach the leads already running"
---------------------------------------------------------
Pinning alone would strand a corrected template behind every in-flight lead. So an execution at
`cursor = k` on version `V` adopts a new version `V'` IFF

    [a.name for a in V'.actions[:k]] == [a.name for a in V.actions[:k]]

Same identities, same order. Field VALUES in the prefix are free to differ — the lead already ran
those actions, so a corrected template there is irrelevant to it. Consequences:

  * Fix month-4's template while a lead sits in month 2 -> prefix untouched -> migrates, gets the fix.
  * Append new steps -> prefix untouched -> migrates.
  * Edit the delay of the Wait a lead is asleep in -> identity and order unchanged -> migrates, and
    its `resume_at` is re-derived from `parked_at` (the edit reaches the sleeping lead).
  * Reorder / delete / insert inside the prefix -> retained on its own version, finishes normally.

Two guarantees fall out, and neither is hoped for:
  * If the first `k` actions match, `V'` has at least `k` actions — the cursor can NEVER point past
    the end. The old "Success, 0 actions" silent death is unrepresentable.
  * Nothing is ever cancelled or orphaned by an EDIT. Only deleting the rule cancels (on_trash).
"""
import frappe
from frappe import _

from tatva_connect.automation.resume import RESUME_DT

DOCTYPE = "CRM Automation Rule Version"

# Volatile / structural columns a frozen definition must never carry: they move on every save without
# changing the program, so hashing them would mint a version per save. `name` is KEPT — it is the
# action's identity. Order is carried by list position, so `idx` is dropped too.
_VOLATILE = frozenset(
	("creation", "modified", "modified_by", "owner", "docstatus", "idx", "parent", "parentfield", "parenttype")
)


# -- payload + identity ------------------------------------------------------


def _freeze(child):
	"""One child row, stripped to its program-relevant columns. `None` normalises to `""` so a field
	that was never set and one that was cleared hash identically."""
	return {k: ("" if v is None else v) for k, v in child.get_valid_dict().items() if k not in _VOLATILE}


def build_payload(rule):
	"""The decision-relevant definition of `rule`, in canonical shape. Pure — no DB writes, no reads."""
	return {
		"on_doctype": rule.on_doctype,
		"event": rule.event,
		"vertical": rule.vertical or "",
		"group": rule.group or "",
		"program": rule.program or "",
		"criteria": [_freeze(c) for c in rule.criteria],
		"actions": [_freeze(a) for a in rule.actions],
	}


def _canonical(payload):
	"""Deterministic serialisation via the native encoder (`frappe.as_json` sorts keys) — the hash input
	and the stored blob are the same bytes. Compact separators, no indent, so the hash is stable."""
	return frappe.as_json(payload, indent=None, separators=(",", ":"))


def definition_hash(payload):
	return frappe.utils.sha256_hash(_canonical(payload))


def ensure_version(rule):
	"""Mint the version for `rule`'s current definition, or reuse the existing one with the same content
	hash (an idempotent save mints nothing; reverting a rule re-flags its old version rather than
	duplicating it). Marks it current, so the hot trigger path resolves the live definition with ONE
	indexed read and never has to re-hash. Returns the version's name."""
	payload = build_payload(rule)
	digest = definition_hash(payload)
	name = frappe.db.get_value(DOCTYPE, {"rule": rule.name, "definition_hash": digest})
	if not name:
		latest = frappe.get_all(
			DOCTYPE, filters={"rule": rule.name}, fields=["version_no"], order_by="version_no desc", limit=1
		)
		name = frappe.get_doc({
			"doctype": DOCTYPE,
			"rule": rule.name,
			"version_no": (latest[0].version_no if latest else 0) + 1,
			"definition_hash": digest,
			"payload_json": _canonical(payload),
			"action_count": len(payload["actions"]),
		}).insert(ignore_permissions=True).name  # authz-ok: tier-a — automation engine: immutable rule version, engine-written
	_mark_current(rule.name, name)
	return name


def _mark_current(rule_name, version_name):
	"""Exactly one current version per rule. `db.set_value` writes the flag directly: the controller's
	immutability guard defends the frozen DEFINITION, and which definition is live is a fact about the
	rule's present, not about this program."""
	for other in frappe.get_all(DOCTYPE, filters={"rule": rule_name, "is_current": 1}, pluck="name"):
		if other != version_name:
			frappe.db.set_value(DOCTYPE, other, "is_current", 0, update_modified=False)
	frappe.db.set_value(DOCTYPE, version_name, "is_current", 1, update_modified=False)


def current_name(rule_name):
	"""The version a NEW execution binds to. One indexed read on the hot trigger path; a rule that
	predates versioning (or was written by a raw db-seed) mints lazily on its first fire."""
	name = frappe.db.get_value(DOCTYPE, {"rule": rule_name, "is_current": 1})
	return name or ensure_version(frappe.get_doc("CRM Automation Rule", rule_name))


# -- reading -----------------------------------------------------------------


def load(version_name):
	"""The frozen definition as `_dict(rule, on_doctype, event, criteria, actions)`, actions rehydrated
	as `frappe._dict` — which returns `None` for a missing attribute exactly like a child doc, so every
	verb handler reads it unchanged. Request-cached: the payload is immutable, so caching is free."""
	cache = frappe.flags.setdefault("_automation_version_cache", {})
	if version_name not in cache:
		row = frappe.db.get_value(DOCTYPE, version_name, ["rule", "payload_json"], as_dict=True)
		if not row:
			frappe.throw(_("Automation rule version {0} no longer exists.").format(version_name))
		payload = frappe.parse_json(row.payload_json)
		cache[version_name] = frappe._dict(
			rule=row.rule,
			on_doctype=payload["on_doctype"],
			event=payload["event"],
			criteria=[frappe._dict(c) for c in payload["criteria"]],
			actions=[frappe._dict(a) for a in payload["actions"]],
		)
	return cache[version_name]


# -- migration ---------------------------------------------------------------


def _shape(action):
	"""An action's IDENTITY for the prefix check: its child-row name AND its verb. Frappe keeps a
	child row's name across an in-place retype (Wait -> Create Note, same row), so name alone would let
	a retyped step pass the prefix check — then `_rescheduled_at` would read a non-Wait as the parked
	Wait, and the execution would silently skip the retyped action. The verb is part of what the
	execution ran, so a changed verb is a changed prefix. Field VALUES stay excluded (already run)."""
	return (action.get("name"), action.get("action_type"))


def prefix_matches(old_actions, new_actions, cursor):
	"""True when the first `cursor` actions are the same actions (name + verb), in the same order.
	Field values are deliberately NOT compared: the execution already ran them."""
	if len(new_actions) < cursor:
		return False
	return [_shape(a) for a in old_actions[:cursor]] == [_shape(a) for a in new_actions[:cursor]]


def _rescheduled_at(old_actions, new_actions, row):
	"""The parked execution's new wake time, or `None` when its Wait's delay is unchanged.

	The Wait it is asleep in is `actions[cursor - 1]` — exact, forever, because the payload is
	immutable. Re-derive from `parked_at` (when it fell asleep), never from now: shortening a wait must
	pull the lead forward, not restart its clock."""
	old_wait, new_wait = old_actions[row.cursor - 1], new_actions[row.cursor - 1]
	if new_wait["wait_expression"] == old_wait["wait_expression"]:
		return None
	from tatva_connect.automation import actions

	return actions.wait_resume_at(new_wait["wait_expression"], frappe.parse_json(row.context_json or "{}"), row.parked_at)


def plan_migration(rule):
	"""Decide, for every Pending execution of `rule`, whether the rule's NEW definition may be adopted —
	and precompute the rescheduled `resume_at` where its Wait's delay changed.

	Pure with respect to the queue (no writes), so `validate()` can run it as a DRY RUN and BLOCK the
	save before it commits when an execution cannot be rescheduled. `on_update()` then applies the very
	same plan. One function, two callers — the decision and the write can never drift."""
	new_actions = build_payload(rule)["actions"]
	rows = frappe.get_all(
		RESUME_DT,
		filters={"rule": rule.name, "status": "Pending"},
		fields=["name", "rule_version", "cursor", "parked_at", "context_json"],
	)
	migrate, retain, unresolvable = [], [], 0
	for row in rows:
		old_actions = load(row.rule_version).actions
		if not prefix_matches(old_actions, new_actions, row.cursor):
			retain.append(row.name)
			continue
		try:
			resume_at = _rescheduled_at(old_actions, new_actions, row)
		except Exception:  # a Wait expression that no longer resolves against this lead's own context
			unresolvable += 1
			continue
		migrate.append(frappe._dict(name=row.name, resume_at=resume_at))

	if unresolvable:
		frappe.throw(
			_("{0} parked lead(s) cannot be rescheduled under this Wait expression — it does not resolve "
			  "against the context they were parked with. Fix the expression, or let them finish first.").format(
				unresolvable
			),
			title=_("Wait cannot be rescheduled"),
		)
	return frappe._dict(migrate=migrate, retain=retain)


def apply_migration(plan, version_name):
	"""Move every migratable execution onto `version_name`, refreshing the `resume_at` cache where its
	inputs changed. Runs inside the rule's own save transaction: the rule and its in-flight leads move
	together, or neither does. Returns `(migrated, retained)`."""
	for row in plan.migrate:
		values = {"rule_version": version_name}
		if row.resume_at:
			values["resume_at"] = row.resume_at
		frappe.db.set_value(RESUME_DT, row.name, values)
	return len(plan.migrate), len(plan.retain)


def retire(rule_name, reason):
	"""Everything the queue must forget when a rule is deleted. The ONE destructive path, reached only
	from the rule's `on_trash` — which Frappe still runs under `delete_doc(..., force=1)` (force bypasses
	the LINK check, not the lifecycle), so the operator db-seed's rule re-create cannot leave an orphan.

	Clears `is_current` first: a deleted rule has no live definition, and the retention sweep never
	reclaims a current version — so without this, every deleted rule would pin one version forever.
	Versions an execution or a Run Log still references survive until those age out; the audit trail
	outlives the rule. Then cancels every Pending execution. Returns the number cancelled."""
	for version in frappe.get_all(DOCTYPE, filters={"rule": rule_name, "is_current": 1}, pluck="name"):
		frappe.db.set_value(DOCTYPE, version, "is_current", 0, update_modified=False)
	names = frappe.get_all(RESUME_DT, filters={"rule": rule_name, "status": "Pending"}, pluck="name")
	for name in names:
		frappe.db.set_value(RESUME_DT, name, {"status": "Cancelled", "status_reason": reason})
	return len(names)
