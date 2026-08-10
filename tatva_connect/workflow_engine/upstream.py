# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""What is available AT a node — values and emitting nodes alike — by walking the graph backwards from it.

An author configuring a Route has to name a field. Until now the only way to do that was to type it,
from memory, with nothing checking the spelling: a typo produced a condition that looked right, never
matched, and reported nothing. The engine already learned this lesson once, for event names — a Wait's
event is picked from a declared list precisely because "a typo there parked a journey for ever with nothing
able to wake it". The same argument applies to every variable name, and this module is that fix.

TWO SOURCES, ONE LIST
---------------------
  * the SUBJECT's own fields, from the Trigger — the lead (or task) the workflow watches, read through
    `automation.describe.builder_schema`, which is already grain-scoped.
  * whatever ANCESTOR nodes write into journey state, from each verb's `emits` declaration — including the
    variables an author named themselves in a Call API's `capture` rows.

Only ancestors count. A value written by a node that journeys after this one, or on a branch this one is not
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
	`{ref, label, type, source}`. One shape whatever the value came from, so the control does not need to
	know whether it is testing a lead field or something an upstream node wrote. Operators are resolved by
	TYPE, through the builder contract's `operators_by_type` — never per field.

	`nodes` is the authored graph the canvas is holding — `[{node_id, node_type, config_json, edges}]` —
	so this answers for work in progress, not only for what is saved.
	"""
	if not frappe.has_permission("CRM Workflow", "read"):
		frappe.throw(_("Not permitted"), frappe.PermissionError)

	nodes = rows_of(nodes)
	if node_id not in {n.get("node_id") for n in nodes}:
		return []
	return with_subject_fields(emitted_at(nodes, node_id), subject_fields_of(nodes))


def rows_of(nodes):
	"""The authored graph as rows, whether it arrived over the wire or from a caller that already has it."""
	return frappe.parse_json(nodes) if isinstance(nodes, str) else (nodes or [])


def emitted_at(nodes, node_id):
	"""The POSITIONAL half of `available_at` — what ancestors of THIS node write into journey state.

	Split out because it is the only half that depends on where the node sits: the other half is the
	subject's own schema, which is a fact about the graph's Trigger and identical at every node. The
	canvas asks for both once per graph rather than the whole answer once per click.
	"""
	nodes = rows_of(nodes)
	by_id = {n.get("node_id"): n for n in nodes if n.get("node_id")}
	if node_id not in by_id:
		return []

	found = []
	seen = set()
	for ancestor_id in _ancestors(by_id, node_id):
		for value in _emitted_by(by_id[ancestor_id]):
			if value["ref"] not in seen:
				seen.add(value["ref"])
				found.append(value)
	return found


def subject_fields_of(nodes):
	"""The GRAPH half of `available_at` — the subject's own fields, one answer for every node in the graph."""
	nodes = rows_of(nodes)
	return _subject_fields({n.get("node_id"): n for n in nodes if n.get("node_id")})


def with_subject_fields(emitted, subject_fields):
	"""The ONE composition rule, so the two halves are only ever put back together one way.

	No de-duplication of the subject's fields against the nodes above is expected to bite: every key is
	`<source>.<field>`, so a Call API's `api.status` and the lead's `crm_lead.status` are two entries and
	two labels. This list used to be de-duplicated by BARE name with the node's value first, which meant a
	node emitting `status` silently ATE the lead's own — the author was offered one `Status`, the wrong
	one, and the predicate they had built at the Trigger could not match at a Route below the call. The
	guard stays because it is what makes the order — emitted first — the answer on a collision.
	"""
	seen = {value["ref"] for value in emitted}
	return [*emitted, *[field for field in subject_fields if field["ref"] not in seen]]


def _ancestors(by_id, node_id):
	"""Every node that has CERTAINLY run by the time `node_id` runs, nearest first.

	NOT "every node that can reach it", which is what this walked until 2026-08-10 while its docstring
	claimed otherwise. The two agree on a straight line and part company the moment a graph branches and
	rejoins: both arms of a Route can reach the join, but exactly ONE of them ran. Offering the other arm's
	values is the silent non-match this module exists to prevent, and `graph._wait_problems` — the gate
	whose whole job is to refuse a Wait on a node that "does not always run before it" — asked this same
	question and so accepted one. A journey down the other arm then parks for ever: no error, no step log,
	no clock.

	The question both were asking is DOMINANCE — a node is certain only if EVERY path from the entry to
	this one passes through it. That is what is computed here, by the standard iterative fixpoint.

	Reachability is kept as the answer for a graph with no entry: mid-authoring, before a Trigger is
	dropped, "certainly ran" has no meaning, and an empty picker at that moment is the very thing the
	module docstring says not to do. Nothing can publish in that state — the gate requires a Trigger — so
	the loose answer is never the one a live workflow is judged by.
	"""
	incoming, outgoing = {}, {}
	for node in by_id.values():
		for edge in node.get("edges") or []:
			target = edge.get("to_node")
			if target:
				incoming.setdefault(target, []).append(node["node_id"])
				outgoing.setdefault(node["node_id"], []).append(target)

	def reachable_backwards():
		order, seen, queue = [], {node_id}, [node_id]
		while queue:
			for parent in incoming.get(queue.pop(0), []):
				if parent not in seen:
					seen.add(parent)
					order.append(parent)
					queue.append(parent)
		return order

	entry = _entry_of(by_id, incoming)
	if not entry or node_id not in by_id:
		return reachable_backwards()

	dominators = _dominators(by_id, incoming, entry)
	certain = dominators.get(node_id, set()) - {node_id}
	# Nearest first, which is the order every caller reads in: the walk backwards already visits by
	# distance, so ordering by it keeps a nearer node's value winning the de-duplication above.
	return [n for n in reachable_backwards() if n in certain]


def _entry_of(by_id, incoming):
	"""Where a journey starts: the Trigger, or the one node nothing points at. `None` when neither is
	answerable, which is an unfinished graph and never a publishable one."""
	triggers = [n for n, node in by_id.items() if node.get("node_type") == registry.TRIGGER]
	if len(triggers) == 1:
		return triggers[0]
	roots = [n for n in by_id if not incoming.get(n)]
	return roots[0] if len(roots) == 1 else None


def _dominators(by_id, incoming, entry):
	"""`{node: the nodes every path from `entry` to it passes through}`, itself included.

	The textbook iterative formulation: everything is assumed to dominate everything until a path proves
	otherwise. A node the entry cannot reach keeps the full set and therefore dominates nothing that
	matters — it can never run, so nothing it emits is ever certain.
	"""
	everything = set(by_id)
	dominators = {n: set(everything) for n in by_id}
	dominators[entry] = {entry}
	changed = True
	while changed:
		changed = False
		for node in by_id:
			if node == entry:
				continue
			parents = incoming.get(node) or []
			found = set(everything)
			for parent in parents:
				found &= dominators[parent]
			found = found | {node} if parents else {node}
			if found != dominators[node]:
				dominators[node] = found
				changed = True
	return dominators


def node_label(node_id, node_type):
	"""How a PERSON is told which node something came from — composed once, for every consumer.

	The author's own node id leads; the type follows so a bare `n3` still says what it is. The inspector
	spelt this itself for the Wait's picker while the value picker read it from here, so one graph could
	show the same node under two spellings.
	"""
	return _("{0} · {1}").format(node_id, _(registry.declaration(node_type)["label"]))


def emitters_at(nodes, node_id):
	"""The nodes a Wait at `node_id` may legitimately wait on, with the outcomes each reports.

	THE SAME `_ancestors` WALK `available_at` USES, and that is the whole point. Position decides what a
	node can see — values AND outcomes — so both answers come out of one walk rather than a JS filter that
	knew nothing about position. The canvas offered every emitting node in the graph including its own
	descendants; publish then refused the result with "does not always run before it".

	Two facts per entry because the inspector renders two pickers from them and they must not disagree:
	the node, and what that node reports. `outcomes` is read from the node's declaration, never from a
	frontend copy of it.

	A node that emits nothing is left out: waiting on it builds a park nothing can ever satisfy.
	"""
	by_id = {n.get("node_id"): n for n in nodes if n.get("node_id")}
	if node_id not in by_id:
		return []

	found = []
	for ancestor_id in _ancestors(by_id, node_id):
		node_type = by_id[ancestor_id].get("node_type")
		outcomes = list(registry.outcomes_for(node_type))
		if outcomes:
			found.append({
				"node_id": ancestor_id,
				"label": node_label(ancestor_id, node_type),
				"outcomes": outcomes,
			})
	return found


def _shaped(name, ftype, label, source, source_label, options=None, emitted=False, pick=None):
	"""One field, in the builder contract's own shape — `{ref, label, type, source, source_label, emitted}`.

	`options` rides along ONLY when the field really has choices, so a Select's predicate value becomes a
	dropdown instead of a free-text box. Absent for everything else, which is why the key is conditional
	rather than a `None` on every row — a node-emitted variable declares no choices and must not claim to.

	Deliberately NO per-field `operators`: the contract resolves them by TYPE, from `operators_by_type`,
	which is composed from the evaluator's own operator families. A per-field list here would be a second
	vocabulary, and the one that already exists (`describe.operators_for`) emits SYMBOLS the evaluator
	rejects outright — offering them would build a predicate that can never match.

	`source` is the namespace a reference is written with (`crm_lead`, `api`); `source_label` is how a
	PERSON is told where the value came from. Both are answered here because only this module knows both —
	deriving the label in the picker would be a second brain guessing that `crm_lead` means the lead, and
	it would guess wrong for every node source, whose name is the author's own.

	`emitted` says WHAT that source is: True when a node in the graph produced this value, False when it is
	a field of the subject. Only this module knows — it is the difference between the two loops in
	`available_at` — and the canvas needs it for both of its questions ("which node produced this value"
	and "is this a subject field, so may the working set narrow it away"). It is UNCONDITIONAL, unlike
	`options`, because absence would have to be read as False and a row that simply forgot to say would be
	silently narrowed away. The canvas used to answer this by scanning the raw graph prop for the id, which
	is C17.1's defect exactly: a backend answer recomputed client-side.
	"""
	shape = {
		"ref": name, "label": label or name, "type": ftype or "Data",
		"source": source, "source_label": source_label or source, "emitted": bool(emitted),
	}
	if options:
		shape["options"] = options
	# `pick` rides on the same terms as `options` and for the same reason: it is `describe._pick_for`'s
	# answer to "what draws a value for this field", and dropping it here is what made a Link a text box.
	if pick:
		shape["pick"] = pick
	return shape


def _emitted_by(node):
	"""The journey-state variables one node writes, from its verb's declaration plus its own config."""
	from tatva_connect.automation import actions

	config = _config(node)
	emitted = actions.emits_of(node.get("node_type"), config)

	emitted = [*emitted, *_declared_writes(node, config)]

	group = node_label(node["node_id"], node["node_type"])
	return [
		_shaped(
			refs.of_node(node["node_id"], value["name"]), value.get("type"), value.get("about"),
			node["node_id"], group, emitted=True,
		)
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
	Wait contributes is exactly what the journey really merges."""
	from tatva_connect.workflow_engine import interpreter

	return {str(key) for key in interpreter.accepts_map(value).values() if key}


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
		yield field, registry.WRITE_KINDS[kind]["keys"](config[field["name"]])


def write_fields_of(node_type, config):
	"""`(field, keys-or-None)` for every field of THIS node type that declares `writes` — the same walk
	`_write_fields` does, addressed by type and config rather than by a node row.

	Exists so the publish-time reserved-name gate reads the keys through the ONE enumerator the journey
	itself uses. A second walk there would let the gate bless a key the journey really merges (or refuse one
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
	for _field, keys in _write_fields(node, config):
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
		keys = {value["ref"] for value in _emitted_by(node)}
		opaque = False
		for ancestor_id in _ancestors(by_id, node_id):
			keys |= {value["ref"] for value in _emitted_by(by_id[ancestor_id])}
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
	return {f["ref"] for f in _subject_fields(by_id)}


def _subject_fields(by_id):
	"""The subject's own fields — ALL of them, because READING is not WRITING.

	This used to offer `builder_schema`, which is the curated allowlist of fields automation may SET. That
	is the right answer for a write target and the wrong one for a read: journey state falls through to the
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
	# The LEAD too, whenever the subject is not itself one: `interpreter._record_loaders` carries it at run
	# time, so offering less here would hide a value the engine really has — and offering more would be the
	# defect this rule exists to prevent. Two records, one list, each row naming its own source.
	found = []
	for doctype in dict.fromkeys([subject, "CRM Lead"]):
		found += [
			_shaped(f["ref"], f["type"], f["label"], refs.slug(doctype), _(doctype), f.get("options"),
			        pick=f.get("pick"))
			for f in refs.readable_for(doctype)
		]
	return found


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
