# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""W8.1 — an authored Update Field node keeps writing exactly what it wrote, through the new rows control.

The node used to declare four params for ONE field (`fieldname`, `value_mode`, and whichever of `value` /
`context_field` / `expression` the mode gated in). It now declares one `updates` field carrying a row per
field, each row holding its own mode. A node still in the old shape would resolve no rows and write
NOTHING — silently, on a live patient's record — which is why this is a patch and not a note.

The fold is exact and loses nothing: one old node becomes one row, the same field, the same mode, and the
value read out of whichever param that mode gated in. The three legacy value params collapse to the row's
one `value` because the mode already says how to read it — that collapse IS the change, and it is the
reason the three `depends_on_value` gates could go.

DECLARES AN END STATE AND ASSUMES NOTHING ABOUT WHAT RAN BEFORE: it reads each node's real config, skips
one that already carries rows, skips one that never named a field, and is a free no-op on a second run and
on a site with no Update Field node at all. No DDL and no schema_setup twin — the config is a JSON column
on `CRM Workflow Node` that a fresh site is born with, and a fresh site has no old-shape row to repair.

WHAT THIS CANNOT REACH, and it is a deploy gate rather than a gap: a `CRM Workflow Version` is
content-addressed and immutable by design (`versions.py`), so a journey parked on a version frozen before
this change executes the frozen node, not the repaired one. `actions._update_rows` raises loudly for that
case rather than writing nothing. The precondition query is in
`docs/plans/workflow-engine/W8-node-completeness.md`; run it before deploying, and if it prints anything,
let those journeys finish first.
"""
import frappe

from tatva_connect.workflow_engine import refs, registry

NODE_DT = "CRM Workflow Node"
NODE_TYPE = "Update Field"

# Which legacy param each mode's value was gated into — the `depends_on_value` map, read backwards.
_VALUE_PARAM = {
	refs.LITERAL: "value",
	refs.FROM_CONTEXT: "context_field",
	refs.EXPRESSION: "expression",
}
_LEGACY = ("fieldname", "value_mode", "value", "context_field", "expression")


def execute():
	if not frappe.db.table_exists(NODE_DT):
		return

	for row in frappe.get_all(NODE_DT, filters={"node_type": NODE_TYPE}, fields=["name", "config_json"]):
		config = registry.config_of(row)
		folded = _folded(config)
		if folded is None:
			continue
		frappe.db.set_value(NODE_DT, row.name, "config_json", frappe.as_json(folded), update_modified=False)


def _folded(config):
	"""The node's config with its one field expressed as a row, or `None` when there is nothing to do."""
	if config.get("updates") or not config.get("fieldname"):
		return None
	mode = config.get("value_mode") or refs.LITERAL
	kept = {k: v for k, v in config.items() if k not in _LEGACY}
	kept["updates"] = [{
		"name": config["fieldname"],
		"mode": mode,
		# An unknown mode keeps its literal, which is what the old runtime did with one: `_resolve_set_field_value` fell through to `return action.value`.
		"value": config.get(_VALUE_PARAM.get(mode, "value")),
	}]
	return kept
