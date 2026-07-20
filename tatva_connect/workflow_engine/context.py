# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Everything a node needs in order to be AUTHORED, answered in one call.

WHY THIS EXISTS
---------------
Authoring used to be served by three unrelated mechanisms: `available_at` for variables,
`builder_schema` for fields and operators, and a per-field `grain_scoped` flag for link filters. Three
callers, three shapes, three chances to forget one — and we did forget. The grain was declared on the
Trigger and reached exactly one picker, because nothing carried it anywhere else. Each leak then got
patched individually, which is how a contract becomes a pile of special cases.

A node does not have "some fields that happen to need scoping". A node sits at a position in a graph, and
that position DETERMINES what it can see: which subject, which grain, which values earlier nodes
produced. That is the contract. This module computes it once, and every control in the inspector reads
from it — so a picker is scoped because of where its node is, not because someone remembered a flag.

WHAT A POSITION DETERMINES
--------------------------
  subject   — the doctype the Trigger watches. Decides what fields exist at all.
  grain     — the vertical/group/program the Trigger declares. Scopes every choice drawn from
              grain-carrying data, everywhere, without being asked.
  variables — what ancestors emit, plus the subject's own readable fields.
  settable  — the fields automation is allowed to WRITE, already grain-scoped by the contract brain.
  operators — the comparison vocabulary, per field type.

Answers for an UNSAVED graph: the picker has to help while the author is still building, and requiring a
save first would leave it empty at exactly the moment it matters.
"""
import frappe
from frappe import _

from tatva_connect.taxonomy.grain import AXES as _GRAIN_AXES
from tatva_connect.workflow_engine import registry, upstream


@frappe.whitelist()
def node_context(nodes, node_id):
	"""The whole authoring contract for one node, in one response.

	One call rather than three: a control cannot be scoped by something it was never handed, and every
	separate call is a place the grain can be dropped.
	"""
	if not frappe.has_permission("CRM Workflow", "read"):
		frappe.throw(_("Not permitted"), frappe.PermissionError)

	nodes = frappe.parse_json(nodes) if isinstance(nodes, str) else (nodes or [])
	trigger = _trigger_config(nodes)
	subject = trigger.get("subject_doctype") or ""
	grain = {axis: trigger.get(axis) for axis in _GRAIN_AXES if trigger.get(axis)}

	from tatva_connect.automation import describe

	schema = describe.builder_schema(on_doctype=subject, **_schema_grain(grain)) if subject else {}
	return {
		"subject": subject,
		"grain": grain,
		"variables": upstream.available_at(nodes, node_id),
		"settable": schema.get("set_targets") or [],
		"operators_by_type": schema.get("operators_by_type") or {},
		"operator_shapes": schema.get("operator_shapes") or {},
	}


def _trigger_config(nodes):
	trigger = next((n for n in nodes if n.get("node_type") == registry.TRIGGER), None)
	if not trigger:
		return {}
	raw = trigger.get("config_json")
	if raw is None:
		return trigger.get("config") or {}
	return frappe.parse_json(raw or "{}") or {}


def _schema_grain(grain):
	"""The grain, in the argument names `builder_schema` already accepts.

	It has always taken these and the inspector has always passed nothing, so the settable-field list was
	never scoped — an author could be offered a field their workflow's grain may not write.
	"""
	return {axis: grain.get(axis) for axis in _GRAIN_AXES}
