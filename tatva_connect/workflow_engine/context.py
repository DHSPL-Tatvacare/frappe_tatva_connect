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
  emitters  — which ancestor NODES report an outcome, and which outcomes. The same question as
              `variables`, asked about events rather than values, off the same ancestor walk.
  settable  — the fields automation is allowed to WRITE, already grain-scoped by the contract brain.
  operators — the comparison vocabulary, per field type.

Everything above is positional, and that is why it is one answer rather than several. The Wait's picker
was the one control that did NOT ask: it filtered the canvas's own graph array on can-emit and not-self,
so it offered a Wait the nodes it blocks. Publish refused what it produced. A control that composes its
own offer is outside this contract however reasonable its filter looks.

Answers for an UNSAVED graph: the picker has to help while the author is still building, and requiring a
save first would leave it empty at exactly the moment it matters.
"""
import frappe
from frappe import _

from tatva_connect.taxonomy.grain import AXES as _GRAIN_AXES
from tatva_connect.workflow_engine import ENGINE_SWITCH, registry, upstream


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
		# W3.1 — the subject fields the author declared this workflow works with, ANSWERED AND NOT APPLIED.
		# `variables` and `settable` below are byte-identical whether or not a set is declared, and that is
		# the whole guarantee: the backend says what was DECLARED, the canvas applies it as a presentation
		# filter. Filtering here would drop an out-of-set saved reference off the wire, and the picker's
		# stale-value prepend — the thing that stops a narrowing silently breaking an existing workflow —
		# would have nothing left to prepend. It stays a config key for the same reason it stays unfiltered:
		# the moment the DISPATCHER selects on it, it needs an indexed column and a back-fill.
		"working_set": trigger.get("working_set") or [],
		"variables": upstream.available_at(nodes, node_id),
		"emitters": upstream.emitters_at(nodes, node_id),
		"settable": schema.get("set_targets") or [],
		"operators_by_type": schema.get("operators_by_type") or {},
		"operator_shapes": schema.get("operator_shapes") or {},
	}


@frappe.whitelist()
def test_call(endpoint, request_body=None, lead=None):
	"""Fire a Call API node's request for real, so an author can map what actually comes back.

	THE SAME PATH A RUN TAKES — `actions._call_endpoint`, unchanged. A preview that built its own request
	would show the author a response the run never receives, which is worse than no preview: it would be
	confidently wrong. Endpoint, method, headers and secret still come off the curated `Webhook`; the
	author supplies only the body, exactly as at runtime.

	Gated on the ENGINE switch, so a site whose automation is dormant makes no outbound call from an
	authoring screen either. Answering `{"armed": False}` rather than throwing lets the control say why.

	It says WHICH lead it used. A response is shaped by the record behind it, and an author reading a tree
	built from a lead they did not choose would map paths that do not exist for the next one.
	"""
	if not frappe.has_permission("CRM Workflow", "write"):
		frappe.throw(_("Not permitted"), frappe.PermissionError)

	from tatva_connect import automation
	from tatva_connect.automation import actions
	from tatva_connect.automation.context import context_for

	if not automation.is_enabled(ENGINE_SWITCH):
		return {"armed": False}

	subject = lead or frappe.db.get_value("CRM Lead", {}, "name", order_by="modified desc")
	if not subject:
		return {"armed": True, "error": _("There is no lead to build a request from yet.")}

	doc = frappe.get_doc("CRM Lead", subject)
	doc.check_permission("read")  # the author sees this record's data in the tree, so they must be able to
	body = actions.build_request_body(request_body, context_for(doc, changed=None)) if request_body else None
	response = actions._call_endpoint(endpoint, doc, body)
	return {"armed": True, "lead": subject, "sent": body, **response}


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
