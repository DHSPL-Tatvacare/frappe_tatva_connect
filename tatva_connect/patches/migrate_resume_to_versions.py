"""Bind every existing `CRM Automation Resume` row to an immutable `CRM Automation Rule Version`.

Before this, a parked execution stored `next_action_idx` — an index into the rule's EFFECT-lane action
list — and re-read that list live from the mutable rule at resume time. Now it stores `cursor`, an index
into a FROZEN, all-lanes action list. The two indexes are not the same number.

The only definition still available for an already-parked row is the rule's CURRENT one, so that is what
we freeze and bind it to. Translation, per row:

    effect_actions = [a for a in rule.actions if lane(a) == "effect"]
    cursor         = raw index of effect_actions[next_action_idx]

`next_action_idx == len(effect_actions)` is legal and means "resume into nothing left" (the old silent
`Success, 0 actions`); it maps to `cursor = len(actions)` and the row completes cleanly on its next sweep.

A row we cannot translate is marked `Failed` and logged. We NEVER guess: a wrongly translated cursor is a
duplicate WhatsApp message to a patient.

Idempotent — rows already carrying a `rule_version` are skipped. Guarded no-op on a fresh install and on
an empty queue. Post-model-sync: it reads `next_action_idx`, which the new JSON has dropped from the
doctype. Model sync ADDS and ALTERS columns but never DROPS them, so the orphan column survives this
migrate (and every future one) until an operator runs `bench trim-database` explicitly. The patch's
correctness depends only on the column existing WHEN it runs, which it does on the deploy that ships this
change; `has_column` guards the case where a manual trim removed it first (then rows stay unbound and the
next sweep marks them Failed via `versions.load`, never silently mis-resumed).
"""
import frappe

_RESUME = "CRM Automation Resume"
_RULE = "CRM Automation Rule"


def execute():
	if not frappe.db.table_exists(_RESUME) or not frappe.db.has_column(_RESUME, "next_action_idx"):
		return  # fresh install, or the column has already been pruned by a later migrate
	rows = frappe.db.get_all(
		_RESUME, filters={"rule_version": ["in", ("", None)]}, fields=["name", "rule", "next_action_idx", "creation"]
	)
	if not rows:
		return

	from tatva_connect.automation import versions

	migrated, failed = 0, 0
	for row in rows:
		cursor = _translate(row)
		if cursor is None:
			frappe.db.set_value(_RESUME, row.name, {
				"status": "Failed",
				"status_reason": "could not be bound to a rule version on upgrade — see the Error Log",
			})
			failed += 1
			continue
		frappe.db.set_value(_RESUME, row.name, {
			"rule_version": versions.ensure_version(frappe.get_doc(_RULE, row.rule)),
			"cursor": cursor,
			"parked_at": row.creation,  # park() inserted the row the same instant it computed resume_at
		})
		migrated += 1

	frappe.db.commit()
	print(f"migrate_resume_to_versions: {migrated} bound to a version, {failed} failed")


def _translate(row):
	"""The old effect-lane index -> the new raw index, or None when it cannot be resolved."""
	from tatva_connect.automation import actions

	try:
		rule = frappe.get_doc(_RULE, row.rule)
	except frappe.DoesNotExistError:
		frappe.log_error(title="automation: resume row has no rule", message=f"resume={row.name} rule={row.rule}")
		return None

	raw_indexes = [
		i for i, a in enumerate(rule.actions)
		if actions._ACTION_LANES.get(a.action_type, (None, None))[0] == "effect"
	]
	old_idx = row.next_action_idx or 0
	# A genuine park always happens AFTER a Wait, so the resume point is past at least one action:
	# next_action_idx is never 0. A 0 would translate to cursor 0, which run_effects treats as a FRESH
	# fire — re-evaluating criteria and re-running the whole pre-wait segment, re-sending any message in
	# it. Refuse it rather than risk a duplicate: mark the row Failed (the caller logs it).
	if old_idx <= 0:
		frappe.log_error(
			title="automation: resume cursor is zero",
			message=f"resume={row.name} rule={row.rule} next_action_idx={old_idx} — refusing to bind a fresh-fire cursor",
		)
		return None
	if old_idx == len(raw_indexes):
		return len(rule.actions)  # parked past the last effect action: nothing left to resume into
	if old_idx < len(raw_indexes):
		return raw_indexes[old_idx]
	frappe.log_error(
		title="automation: resume cursor out of range",
		message=f"resume={row.name} rule={row.rule} next_action_idx={old_idx} effect_actions={len(raw_indexes)}",
	)
	return None
