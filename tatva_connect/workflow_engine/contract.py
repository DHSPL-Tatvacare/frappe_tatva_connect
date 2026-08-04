# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""What a node READS — the other half of the node contract.

WHY THIS EXISTS
---------------
A verb declares what it EMITS, and `upstream.available_at` turns those declarations into the list of
values a node may read. Both halves existed; nothing joined them. So the knowledge of what was readable
was wired to the authoring picker and to nothing else, and publish had no opinion at all about whether a
node's references could be satisfied.

The result was a workflow that published green and then failed on a live patient record: a predicate
naming a variable no upstream node writes raises at evaluation and marks the journey Failed, while a
`Variable` field naming the same missing key fails SILENTLY — the assignee resolves to None and the node
leaves by `nobody`, the due date resolves to None and the task quietly takes its default, the value
written onto a field is None. No log distinguishes any of that from a workflow behaving correctly.

This module answers "what does this node reference?" so `graph.problems` can answer "and is any of it
unavailable here?" before the workflow ever runs.

DERIVED FROM THE DECLARATION, NEVER FROM A LIST OF NODE TYPES
------------------------------------------------------------
Every reference is found by walking the node's DECLARED config fields — `registry.config_fields`, which
already covers core node types and verb params alike, because a verb's params become a node type's
config. A field says how it should be read, in one of four ways, and adding a fifth field to a verb needs
no change here:

  Variable          the value IS a variable name
  Predicate         every rule in the tree names the field it tests
  reads=expression  a Python expression, read through `expr.context_keys`
  reads=ctx_json    a JSON map whose values may be `$ctx.<name>`
  reads=value_rows  rows of {name, mode, value}; a `From Context` row's value IS a variable name

A field declaring none of these reads nothing. That is the default, and it is the honest one: a literal
subject line or a note is text, not a reference.
"""
import frappe
from frappe.utils import flt

from tatva_connect.workflow_engine import refs, registry

_CTX_PREFIX = refs.CTX_PREFIX

# A `value_rows` row reads journey state only in this mode. RE-EXPOSED, never re-declared: `sends` and
# `registry` read these off `contract`, and the same objects keep them working while W5.4 leaves exactly
# one place where the words are written down.
FROM_CONTEXT = refs.FROM_CONTEXT
LITERAL = refs.LITERAL


def reads_of(node_type, config):
	"""Every journey-state value this node references, as `{ref, field, label}` — the SAME word `upstream` uses for what a node writes, because it is the same string.

	`field` and `label` travel with the name so a problem can be anchored on the control that carries the
	bad reference rather than on the node as a whole — the author needs to know WHICH box to fix.
	"""
	config = config or {}
	found = []
	for field in registry.config_fields(node_type):
		value = config.get(field["name"])
		if not value:
			continue
		for name in sorted(_references(field, value)):
			found.append({"ref": name, "field": field["name"], "label": field["label"]})
	return found


def _references(field, value):
	"""The names one configured field references — resolved through the ONE table.

	A type that can only read one way carries its kind in `FIELD_TYPES`; a type whose control serves
	several semantics (`Code`, `Data`) carries the kind on the FIELD. `registry.read_kind_of` answers
	which, so this function no longer switches on a type name and a new kind is one row elsewhere.

	Never raises: a malformed expression or a broken JSON map is somebody else's problem to report, and
	losing the whole publish check to it would be worse.
	"""
	from tatva_connect.workflow_engine import registry

	kind = registry.read_kind_of(field)
	if not kind:
		return set()
	try:
		return registry.READ_KINDS[kind]["keys"](value)
	except Exception:
		return set()


def value_row_keys(rows):
	"""The journey-state names a `value_rows` field references — its `From Context` rows, and only those.

	Added because a WhatsApp template's placeholders were an ENTIRELY UNDECLARED read surface: the node's
	only param was a Link to the template, `sends.send_whatsapp` then did `ctx.get(name)` for every
	placeholder the provider declared, and none of it was visible here. Publish therefore checked nothing,
	and a template with a `{{patient_name}}` slot that nothing upstream produces sent "Hi ," to a real
	patient with nothing in the step log to say so.

	Declared as a generic `reads` kind rather than as a Send WhatsApp special case: any verb that takes
	rows of "this slot gets that value" is covered by declaring the kind on its field.
	"""
	found = set()
	for row in rows or []:
		if not isinstance(row, dict) or row.get("mode") != FROM_CONTEXT:
			continue
		if row.get("value"):
			found.add(str(row["value"]))
	return found


def value_rows_map(rows):
	"""`{slot: (mode, value)}` — the declared rows as a lookup, so the runtime reads them the ONE way.

	Lives here, beside the function that decides what those rows REFERENCE, because the gate and the
	sender must agree on the row shape or the check guards a shape nobody sends.
	"""
	return {
		str(row["name"]): (row.get("mode") or LITERAL, row.get("value"))
		for row in rows or []
		if isinstance(row, dict) and row.get("name")
	}


# The absence of a write target, distinguishable from a target whose value is genuinely None or 0.
_NO_TARGET = object()


def resolve_row(mode, value, ctx, current=_NO_TARGET):
	"""A declared row's `(mode, value)` becomes a value. THE one reader — the twin of `value_rows_map`.

	It was written FOUR times: `sends._template_parameters`, `sends._slot_values` and
	`sends._agent_variables` held a byte-identical `ctx.get(value) if mode == FROM_CONTEXT else value`, and
	`actions._resolve_set_field_value` decided the same thing in different words with a third branch. Four
	copies is four chances for a renamed mode to fall through to the literal branch — which writes the
	author's VARIABLE NAME onto a patient's field, or sends it to them as text.

	`current` is the write target's value NOW, and only `Increment by` reads it. A caller that has no
	target passes nothing and the mode is REFUSED rather than treated as a literal: a send verb filling a
	template slot has no field to add to, and the silent alternative is a patient receiving the digit the
	author meant as a step. That refusal is the whole reason this parameter is a sentinel and not `None` —
	a counter really can be sitting at `None`.
	"""
	if mode == refs.FROM_CONTEXT:
		return ctx.get(value)
	if mode == refs.EXPRESSION:
		from tatva_connect.automation import expr

		return expr.resolve_expression(value, ctx)
	if mode == refs.INCREMENT:
		if current is _NO_TARGET:
			raise ValueError(f"{refs.INCREMENT} adds to a field's own value, so it cannot fill a template slot")
		return flt(current) + flt(value)
	return value


def _predicate_fields(tree, depth=0):
	"""Every field a predicate tree tests, at any depth."""
	if depth > 20 or not isinstance(tree, dict):
		return set()
	found = set()
	if tree.get("field"):
		found.add(tree["field"])
	for child in tree.get("children") or []:
		found |= _predicate_fields(child, depth + 1)
	return found


def _expression_keys(value):
	from tatva_connect.automation import expr

	try:
		return expr.context_keys(value)
	except (SyntaxError, ValueError, TypeError):
		return set()  # a broken expression is reported by the evaluator's own validator


def _ctx_json_keys(value):
	"""Every `$ctx.` reference a JSON map names, AT ANY DEPTH.

	Asked of `actions`, which owns the walk the runtime itself performs. This used to iterate
	`data.values()` — one level — which was true enough for a child row's flat field map and became a hole
	the moment a node carried a real API body: `{"messages": [{"content": "$ctx.…"}]}` nests its reference
	inside a list of objects, so the gate saw nothing, published green, and the journey then sent the literal
	string `$ctx.crm_lead.first_name` to a live provider.

	One walk, two callers — locked by `test_the_gate_sees_every_reference_the_runtime_will_resolve`.
	"""
	from tatva_connect.automation import actions

	raw = value if isinstance(value, str) else frappe.as_json(value)
	try:
		return set(actions.body_references(raw))
	except ValueError:
		return set()  # invalid JSON is the author's own error, reported by the field's validator
