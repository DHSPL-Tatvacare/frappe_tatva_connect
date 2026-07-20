# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""What values are available AT a node — collected by walking the graph backwards from it.

An author configuring a Branch has to name a field. Until now the only way to do that was to type it,
from memory, with nothing checking the spelling: a typo produced a condition that looked right, never
matched, and reported nothing. The engine already learned this lesson once, for event names — a Wait's
event is picked from a declared list precisely because "a typo there parked a run for ever with nothing
able to wake it". The same argument applies to every variable name, and this module is that fix.

TWO SOURCES, ONE LIST
---------------------
  * the SUBJECT's own fields, from the Trigger — the lead (or task) the workflow watches, read through
    `automation.describe.builder_schema`, which is already grain-scoped and allowlist-filtered.
  * whatever ANCESTOR nodes write into run state, from each verb's `emits` declaration — including the
    variables an author named themselves in a Call API's `capture` rows.

Only ancestors count. A value written by a node that runs after this one, or on a branch this one is not
reachable from, is not available here — offering it would be inviting exactly the silent non-match this
module exists to prevent.

Works on an UNSAVED graph. The whole point is to help while the author is still building, and requiring a
save first would leave the picker empty at the only moment it matters.
"""
import frappe
from frappe import _

from tatva_connect.workflow_engine import refs, registry


@frappe.whitelist()
def available_at(nodes, node_id):
	"""Every value a node may read, in the SAME field shape the predicate control already consumes:
	`{key, label, type, source}`. One shape whatever the value came from, so the control does not need to
	know whether it is testing a lead field or something an upstream node wrote. Operators are resolved by
	TYPE, through the builder contract's `operators_by_type` — never per field.

	`nodes` is the authored graph the canvas is holding — `[{node_id, node_type, config_json, edges}]` —
	so this answers for work in progress, not only for what is saved.
	"""
	if not frappe.has_permission("CRM Workflow", "read"):
		frappe.throw(_("Not permitted"), frappe.PermissionError)

	nodes = frappe.parse_json(nodes) if isinstance(nodes, str) else (nodes or [])
	by_id = {n.get("node_id"): n for n in nodes if n.get("node_id")}
	if node_id not in by_id:
		return []

	found = []
	seen = set()
	for ancestor_id in _ancestors(by_id, node_id):
		for value in _emitted_by(by_id[ancestor_id]):
			if value["key"] not in seen:
				seen.add(value["key"])
				found.append(value)

	# The subject's own fields, and no de-duplication against the nodes above: every key here is
	# `<source>.<field>`, so a Call API's `api.status` and the lead's `crm_lead.status` are two entries and
	# two labels. This list used to be de-duplicated by BARE name with the node's value first, which meant a
	# node emitting `status` silently ATE the lead's own — the author was offered one `Status`, the wrong
	# one, and the predicate they had built at the Trigger could not match at a Branch below the call.
	for field in _subject_fields(by_id):
		if field["key"] not in seen:
			seen.add(field["key"])
			found.append(field)
	return found


def _ancestors(by_id, node_id):
	"""Every node that can reach `node_id`, nearest first.

	Walked backwards over the edges rather than forwards from the Trigger: what matters is not what the
	graph contains but what has certainly already run by the time this node does.
	"""
	incoming = {}
	for node in by_id.values():
		for edge in node.get("edges") or []:
			target = edge.get("to_node")
			if target:
				incoming.setdefault(target, []).append(node["node_id"])

	order, seen, queue = [], {node_id}, [node_id]
	while queue:
		for parent in incoming.get(queue.pop(0), []):
			if parent not in seen:
				seen.add(parent)
				order.append(parent)
				queue.append(parent)
	return order


def _shaped(name, ftype, label, source):
	"""One field, in the builder contract's own shape — `{key, label, type}`.

	Deliberately NO per-field `operators`: the contract resolves them by TYPE, from `operators_by_type`,
	which is composed from the evaluator's own operator families. A per-field list here would be a second
	vocabulary, and the one that already exists (`describe.operators_for`) emits SYMBOLS the evaluator
	rejects outright — offering them would build a predicate that can never match.
	"""
	return {"key": name, "label": label or name, "type": ftype or "Data", "source": source}


def _emitted_by(node):
	"""The run-state variables one node writes, from its verb's declaration plus its own config."""
	from tatva_connect.automation import actions

	config = _config(node)
	emitted = actions.emits_of(node.get("node_type"), config)

	emitted = [*emitted, *_declared_writes(node, config)]

	return [
		_shaped(refs.of_node(node["node_id"], value["name"]), value.get("type"), value.get("about"), node["node_id"])
		for value in emitted
	]


def _expression_dict_keys(value):
	"""A Set Variables node's `assign`. Its expression is nearly always the dict literal every author
	writes (`{"stage": "Qualified"}`), and those keys are knowable without evaluating it. `None` means
	they cannot be enumerated — a computed key, a function call."""
	from tatva_connect.automation import expr

	try:
		return expr.dict_literal_keys(value)
	except (SyntaxError, ValueError, TypeError):
		return None


def _payload_map_keys(value):
	"""A Wait's `accepts`. The map is `{dotted payload path: state key}`, so what it WRITES is its
	values — read through the interpreter's own parser, never a second one, so what the gate believes a
	Wait contributes is exactly what the run really merges."""
	from tatva_connect.workflow_engine import interpreter

	return {str(key) for key in interpreter.accepts_map(value).values() if key}


# How to enumerate the keys a field declaring `writes=<kind>` contributes. ONE mechanism, one resolver
# per kind: a node type declares the kind on the field and needs no other change anywhere. A resolver
# returning None means "cannot be enumerated", which makes the node an opaque writer.
_WRITE_KINDS = {
	"expression_dict": _expression_dict_keys,
	"payload_map": _payload_map_keys,
}


def _write_fields(node, config):
	"""Every configured field on this node that declares `writes`, with the keys it contributes.

	Yields `(field, keys-or-None)` so the two questions asked of a writing field — WHICH keys, and can
	they be enumerated at all — are answered by one walk. They used to be two near-identical loops, and a
	second `writes` kind would have had to be added to both.
	"""
	for field in registry.config_fields(node.get("node_type")):
		kind = field.get("writes")
		if not kind or not config.get(field["name"]):
			continue
		yield field, _WRITE_KINDS[kind](config[field["name"]])


def write_fields_of(node_type, config):
	"""`(field, keys-or-None)` for every field of THIS node type that declares `writes` — the same walk
	`_write_fields` does, addressed by type and config rather than by a node row.

	Exists so the publish-time reserved-name gate reads the keys through the ONE enumerator the run
	itself uses. A second walk there would let the gate bless a key the run really merges (or refuse one
	it does not), which is the whole failure class this declaration was added to close.
	"""
	return _write_fields({"node_type": node_type}, config or {})


def _declared_writes(node, config):
	"""The keys the fields of this node declare they write.

	When they cannot be enumerated the node is offered by its own name and marked `opaque`, which tells
	the publish check that absence cannot be proven downstream. Guessing "it contributes nothing" there
	would reject correct workflows.
	"""
	found = []
	for field, keys in _write_fields(node, config):
		if keys is None:
			found.append({
				"name": "*", "type": "Data", "opaque": True,
				"about": _("values set by this node — named in its expression"),
			})
		else:
			found += [{"name": k, "type": "Data", "about": _("set by this node")} for k in sorted(keys)]
	return found


def _opaque_writer(node):
	"""True when this node writes values that cannot be named without running it.

	Asked separately rather than carried on the emitted field: the wire shape of a value is `{key, label,
	type, source}` and is consumed by the picker, so an engine-only flag does not belong in it.
	"""
	return any(keys is None for _field, keys in _write_fields(node, _config(node)))


def available_map(nodes):
	"""`{node_id: set-of-readable-keys}` for a whole graph, plus the ids of nodes whose contribution
	cannot be enumerated.

	Built once for the graph rather than per node: `available_at` reaches the subject's builder schema on
	every call, and asking it twenty times to publish a twenty-node workflow is twenty identical queries.
	Returns `(available, opaque_after)` — `opaque_after` holds the ids of nodes DOWNSTREAM of an opaque
	writer, where a missing reference cannot honestly be called missing.

	A node's OWN emitted values are included here and NOT in `available_at`, and the difference is the
	question each answers. `available_at` answers "what may an author PICK at this node", where a value the
	node has not produced yet would be a lie. This answers "can this node's own configuration be
	satisfied", and a node may legitimately judge its own result: a Call API's `success_when` is evaluated
	against the response that same node just received. Without this the gate demanded an upstream producer
	for `api.status` and refused every Call API that declares when it succeeded.
	"""
	by_id = {n.get("node_id"): n for n in nodes if n.get("node_id")}
	subject = _subject_readable(by_id)

	available, opaque_after = {}, set()
	for node_id, node in by_id.items():
		keys = {value["key"] for value in _emitted_by(node)}
		opaque = False
		for ancestor_id in _ancestors(by_id, node_id):
			keys |= {value["key"] for value in _emitted_by(by_id[ancestor_id])}
			opaque = opaque or _opaque_writer(by_id[ancestor_id])
		available[node_id] = keys | subject
		if opaque:
			opaque_after.add(node_id)
	return available, opaque_after


def _subject_readable(by_id):
	"""Every key the subject really answers to at runtime — its whole schema, not the picker's list.

	Deliberately NOT `builder_schema`. That is the CURATED list an operator has enabled for automation,
	and it is the right answer for what to OFFER an author. It is the wrong answer for what EXISTS: run
	state falls through to the live document, so a node reading any field of the lead reads it fine
	whether or not the field is on the allowlist. Using the curated list here would have made publish
	reject every predicate on a site whose allowlist is not seeded yet — which is every fresh site, and
	is exactly what the first run of this check did.
	"""
	return {f["key"] for f in _subject_fields(by_id)}


def _subject_fields(by_id):
	"""The subject's own fields — ALL of them, because READING is not WRITING.

	This used to offer `builder_schema`, which is the curated allowlist of fields automation may SET. That
	is the right answer for a write target and the wrong one for a read: run state falls through to the
	live document, so a condition or a template value may read any field the record has, allowlist or not.
	The gate already knew this — `_subject_readable` has always allowed the whole schema — so the picker
	offered a fraction of what publish accepted, and on a site whose write allowlist is unseeded it
	offered NOTHING. An author could not branch on `status` or map a patient's name into a template.

	Two questions, one brain: what may be READ is the subject's schema; what may be WRITTEN stays
	`fields.is_settable`.

	The schema itself is `refs.readable_for`, which delegates to `describe.fields_for_doctype` — the same
	brain the criteria builder and the rule validator read, and the only one that knows a CRM Task answers
	to its activity-schema fields by LOGICAL name. This function used to walk `frappe.get_meta` itself, so
	a Task-subject workflow was offered the nine generic `custom_key_date_*` slots and none of the fields
	they actually carry.
	"""
	subject = _subject_doctype(by_id)
	if not subject or not frappe.db.exists("DocType", subject):
		return []
	return [
		_shaped(f["key"], f["type"], f["label"], refs.slug(subject)) for f in refs.readable_for(subject)
	]


def _subject_doctype(by_id):
	"""The doctype the Trigger watches — the ONE reader, shared by both subject questions."""
	trigger = next((n for n in by_id.values() if n.get("node_type") == registry.TRIGGER), None)
	return _config(trigger).get("subject_doctype") if trigger else None


def _config(node):
	if not node:
		return {}
	raw = node.get("config_json")
	if raw is None:
		return node.get("config") or {}
	return frappe.parse_json(raw or "{}") or {}
