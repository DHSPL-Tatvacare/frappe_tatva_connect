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
naming a variable no upstream node writes raises at evaluation and marks the run Failed, while a
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

from tatva_connect.workflow_engine import refs, registry

_CTX_PREFIX = "$ctx."

# A `value_rows` row reads run state only in this mode. The vocabulary is `Update Field.value_mode`'s,
# deliberately — one word for one idea across every verb that can take a value from either place.
FROM_CONTEXT = "From Context"
LITERAL = "Literal"


def is_free_text_reference(value) -> bool:
	"""Is this free-text value a REFERENCE to run state, or a literal the author typed?

	THE one predicate, and it must stay the one predicate. `graph._reference_problems` uses it to decide
	whether to demand an upstream producer, and `actions._action_send_email` uses it to decide whether to
	resolve the value or send it as typed. When those two disagree the gate certifies a configuration the
	runtime then gets wrong — which is exactly what `email_recipient` did: declared a Variable, enforced as
	a Variable, and then handed to `frappe.sendmail` as a literal address.

	`ops@tatvacare.in` cannot be a reference, so it is a literal. `crm_lead.email_id` is one, so it is a
	reference. Locked by `test_send_routing.TestRecipientDeclarationMatchesRuntime`.

	The shape question belongs to `refs`, which is the one module allowed to split on a dot — and it has to
	be asked there, because a bare identifier is no longer a reference at all. Every value now says where
	it came from, so `escalation_email` names nothing and is a literal; a reference carries its source.
	"""
	return refs.is_reference(value)


def reads_of(node_type, config):
	"""Every run-state value this node references, as `{name, field, label}`.

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
			found.append({"name": name, "field": field["name"], "label": field["label"]})
	return found


def _references(field, value):
	"""The names one configured field references. Never raises: a malformed expression or a broken JSON
	map is somebody else's problem to report, and losing the whole publish check to it would be worse."""
	if field["type"] == "Variable":
		if not isinstance(value, str):
			return set()
		if field.get("free_text") and not is_free_text_reference(value):
			return set()
		return {value}

	if field["type"] == "Predicate":
		return _predicate_fields(value)

	reads = field.get("reads")
	if reads == "expression":
		return _expression_keys(value)
	if reads == "ctx_json":
		return _ctx_json_keys(value)
	if reads == "value_rows":
		return value_row_keys(value)
	return set()


def value_row_keys(rows):
	"""The run-state names a `value_rows` field references — its `From Context` rows, and only those.

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
	data = frappe.parse_json(value) if isinstance(value, str) else value
	if not isinstance(data, dict):
		return set()
	return {
		v[len(_CTX_PREFIX):]
		for v in data.values()
		if isinstance(v, str) and v.startswith(_CTX_PREFIX) and v[len(_CTX_PREFIX):]
	}
