# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""A2 — an authored Wait keeps waiting exactly as long as it did, through the two settings that replaced one.

A Wait declared ONE field, `expression`, gated on three modes at once: it was the delay of a For Duration,
the timeout of an Event-or-Timeout, and the instant of an Until Time. One field cannot be three controls,
so it was a bare text box that silently demanded a Python dict literal, and an author who typed `2 minutes`
learnt the contract from a runtime throw. It is now `duration` (a length of time) and `until_time` (an
instant, carrying its own mode). A node left in the old shape would resolve NO wake time — a patient's
journey parked for ever with no clock — which is why this is a patch and not a note.

The fold is exact and loses nothing. A delay moves across verbatim, because the stored form did not change:
`wait_resume_at` still receives the same `add_to_date` kwargs it always did. An instant is folded to
`Expression` mode, which `contract.as_expression` passes through untouched — so whatever an author wrote,
including `add_days(ctx["crm_lead.custom_review_date"], 3)`, is still exactly what runs. DELIBERATELY not
parsed into the friendlier Calendar or From-Context modes: reading an author's expression to decide what
they meant is a guess, and a guess here changes when a real patient is contacted. Those two modes are for
what is authored NEXT; this only has to keep what exists identical, and it does.

DECLARES AN END STATE AND ASSUMES NOTHING ABOUT WHAT RAN BEFORE: it reads each node's real config, skips
one already carrying either new setting, skips one that never named a time, and is a free no-op on a second
run and on a site with no Wait at all. No DDL and no `schema_setup` twin — the config is a JSON column on
`CRM Workflow Node` that a fresh site is born with, and a fresh site has no old-shape row to repair.

WHAT THIS CANNOT REACH, and it is a deploy gate rather than a gap: a `CRM Workflow Version` is
content-addressed and immutable by design (`versions.py`), so a journey parked on a version frozen before
this change executes the frozen node. `interpreter._wait_when` raises `_Permanent` for that case — the
journey is marked Failed where somebody sees it — rather than parking it silently with no clock.
"""
import frappe

from tatva_connect.workflow_engine import refs, registry

NODE_DT = "CRM Workflow Node"
NODE_TYPE = "Wait"
LEGACY = "expression"


def execute():
	if not frappe.db.table_exists(NODE_DT):
		return

	for row in frappe.get_all(NODE_DT, filters={"node_type": NODE_TYPE}, fields=["name", "config_json"]):
		config = registry.config_of(row)
		split = _split(config)
		if split is None:
			continue
		frappe.db.set_value(NODE_DT, row.name, "config_json", frappe.as_json(split), update_modified=False)


def _split(config):
	"""The node's config with its one time setting moved to the one its mode asks for, or `None` to skip."""
	if config.get("duration") or config.get("until_time") or not config.get(LEGACY):
		return None
	kept = {k: v for k, v in config.items() if k != LEGACY}
	if config.get("mode") == registry.UNTIL_TIME:
		kept["until_time"] = {"mode": refs.EXPRESSION, "value": config[LEGACY]}
	else:
		kept["duration"] = config[LEGACY]
	return kept
