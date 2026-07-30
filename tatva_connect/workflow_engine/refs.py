# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The value contract: every value carries where it came from — `<source>.<field>`.

THE ONE MODULE ALLOWED TO SPLIT ON A DOT
----------------------------------------
A reference is `<source>.<field>` and nothing else parses it. Four sources, one grammar:

    crm_lead.status          a record column, slugged with `frappe.scrub`
    crm_task.chemo_date      an activity field, named LOGICALLY — the generic slot never appears
    call_api_1.status        what a node wrote, under its own node id
    _engine.token            the engine's own bookkeeping

Collision is impossible by construction. It used to be impossible only by luck: values lived in one flat
bag, `upstream.available_at` de-duplicated by bare key with node values first, and a Call API's `status`
therefore ATE the lead's `status` — one label, one entry, the author never told which one they had picked,
and a predicate that matched at the Trigger could not match at a Route below the call.

WHY THE FIELD, NOT THE SLOT
---------------------------
An activity value is homed by its DECLARATION, not by a fixed column — a retained common column on the
task row, or the section row that addresses it. A physical address therefore means nothing to an author and
resolves differently per task type. The contract names the LOGICAL field, and both halves of that name are
answered by the ONE declaration
(`CRM Task Type Field`) rather than re-derived here:

  * what an author may NAME — `automation.describe.fields_for_doctype`, which unions the doctype meta
    with every distinct activity-schema fieldname. Measured on this bench: 37 meta fields, 253 in the
    brain, so 218 logical fields exist that are not columns of the doctype at all.
  * what a name RESOLVES to at runtime — `automation.context.activity_values`, which delegates to
    `activity.api._task_values`, keyed by the SAME logical fieldname.

Both sides therefore key on the schema fieldname and cannot drift apart. `_task_values` reads a value back
at the address `activity.api.field_target` names — the same seam every writer wrote by — since Phase 4 of
`docs/plans/task-form-layer/2026-07-25-task-slots-to-sections-and-form-layer.md`, locked by
`tests/activity/test_field_routing_coherence.py`.

RESOLUTION ORDER, AND WHY IT ENDS IN A RAISE
--------------------------------------------
node bucket → reachable record (loaded ONCE, lazily, per segment) → **raise**.

An unresolvable reference is never `None`. A silent `None` is the exact failure class this whole effort
removed everywhere else and it had survived here: an assignee that resolved to nothing left by `nobody`,
a due date quietly took its default, a template slot quietly rendered blank — on a real patient's record,
with nothing in the step log to tell any of it apart from a workflow behaving correctly.

THE `__before` PAIR
-------------------
`changed to` and `changed from…to` read the value a watched field held BEFORE this save. The suffix goes
on the FIELD, never on the source: `crm_lead.status__before`, not `crm_lead__before.status`. Two reasons,
and both are structural rather than stylistic. The before-value is a value OF that record — it belongs in
that record's namespace, and a second source name would invent a writer no node ever writes as. And
`parse` splits once from the LEFT, so `<ref>__before` is still one reference of the same source with a
longer field name: `rules._changed_match` composes it as `f"{field}__before"` and keeps working on a
namespaced field with no change at all. `context_for` writes exactly this key, and it is the only place
it is written.
"""
import re

import frappe

SEP = "."

# The before-value suffix. Declared here because `refs` owns reference GRAMMAR and this is part of it —
# `context.context_for` writes it and `rules._changed_match` composes it, and neither may spell it itself.
BEFORE = "__before"

# A source is a node id or a doctype slug. Both are `frappe.scrub`-shaped, so both are plain identifiers.
# Enforced rather than assumed: without it `ops@tatvacare.in` parses as source `ops@tatvacare`, field
# `in` — and the publish gate would then demand an upstream producer for every literal
# email address an author types.
_SOURCE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# The engine's own namespace. Replaces the four scattered reserved names (`_token`, `_emitted`, `_corr`,
# `_output`): an author's value is written under its NODE, so it can no longer land on engine bookkeeping
# at all. Structural, not a check.
# The prefix marking a value inside a JSON map as a journey-state reference. Declared HERE because both the
# contract and the action layer spell it, and contract imports registry which builds itself from actions.
CTX_PREFIX = "$ctx."

# W5.4 — HOW A VALUE IS FILLED, declared once. `Literal` is what the author typed; `From Context` names
# journey state; `Expression` computes from it. This is the same distinction `refs` already owns — a value
# and where it came from — and the same reason `CTX_PREFIX` lives here: the contract and the action layer
# both spell it, and `contract` imports `registry` which builds itself from `actions`.
# These are the words an AUTHOR PICKS from a Select, so a rename here must reach every runtime `==` or a
# Set Field silently writes the variable's NAME onto a patient's field and a task takes its default date.
LITERAL = "Literal"
FROM_CONTEXT = "From Context"
EXPRESSION = "Expression"

ENGINE = "_engine"

TOKEN = f"{ENGINE}{SEP}token"
EMITTED = f"{ENGINE}{SEP}emitted"
CORRELATION = f"{ENGINE}{SEP}correlation"
OUTPUT = f"{ENGINE}{SEP}output"


class UnknownReference(KeyError):
	"""A reference nothing answers. Loud, because the quiet version is the bug this contract removes."""


def parse(ref):
	"""`(source, field)` for a well-formed reference, or `None`. `None` means MALFORMED — never a guess.

	Split once from the left: a source is a node id or a doctype slug (both `frappe.scrub`-shaped, so
	neither contains a dot), and everything after the first dot is the field. That keeps a dotted payload
	key readable as one field rather than silently re-homed under a source that does not exist.
	"""
	if not isinstance(ref, str) or SEP not in ref:
		return None
	source, _, field = ref.partition(SEP)
	if not field or not _SOURCE.match(source):
		return None
	return source, field


def is_reference(value):
	"""True iff this free text is a well-formed reference rather than a literal the author typed."""
	return parse(value) is not None


def before(ref):
	"""The reference carrying what `ref` held before this save — see the module docstring."""
	return f"{ref}{BEFORE}"


def of_node(node_id, field):
	"""The reference for a value a node wrote."""
	return f"{node_id}{SEP}{field}"


def of_record(doctype, field):
	"""The reference for a record column or an activity field. `frappe.scrub` owns the slug — Frappe's own
	doctype-to-identifier rule, so nothing here has to agree with a second one."""
	return f"{frappe.scrub(doctype)}{SEP}{field}"


def of_engine(key):
	return f"{ENGINE}{SEP}{key}"


def slug(doctype):
	return frappe.scrub(doctype)


def resolve(ref, ctx):
	"""The ONE reader. Raises `UnknownReference` when nothing answers — see the module docstring."""
	return ctx[ref]


def readable_for(doctype):
	"""Every value a record answers to, as `[{ref, label, type}]` — NAMESPACED.

	THE SEAM: describe's BARE `key` goes in, a `ref` comes out. Two different concepts wore one word here.

	Delegates, never re-derives: `automation.describe.fields_for_doctype` is the brain that walks a
	doctype's meta AND — for `CRM Task` alone — unions it with every distinct activity-schema fieldname by
	LOGICAL name. Proven on this bench: `CRM Task` gains 218 logical fields (`outcome`,
	`chemo_rescheduled_date_time`, …) that are not columns of the doctype at all.

	The four keys every document answers to and no doctype declares are appended, because a predicate on
	`creation` or `owner` is legitimate and the meta does not carry them.

	`options` — a Select's real choices, a Link's target doctype — is CARRIED, not re-derived. It was
	dropped here, so a predicate on a Select field offered a free-text box and an author typed a value
	that could never match. It is describe's answer and it crosses the seam beside the `type` it belongs
	to; the IDENTITY still changes (bare `key` in, namespaced `ref` out) and nothing else does.
	"""
	if not doctype:
		return []
	found = [
		{"ref": of_record(doctype, f["key"]), "label": f["label"], "type": f["type"], "options": f.get("options")}
		for f in _describe().fields_for_doctype(doctype)
	]
	return found + [
		{"ref": of_record(doctype, key), "label": label, "type": ftype}
		for key, ftype, label in _RECORD_KEYS
	]


# Every document answers to these and none of them is declared as a field on the doctype.
_RECORD_KEYS = (
	("name", "Data", "ID"),
	("owner", "Link", "Created By"),
	("creation", "Datetime", "Created On"),
	("modified", "Datetime", "Last Modified"),
)


def _describe():
	from tatva_connect.automation import describe

	return describe


class Values:
	"""Journey state and trigger context, as ONE object — a real mapping over namespaced references.

	THE SAME TYPE IN BOTH PLACES, DELIBERATELY. A Trigger predicate is judged at dispatch against the
	triggering document; a Route predicate is judged at execution against the journey. Two builders, two
	shapes and one predicate control is how the Trigger and the Route came to mean different things by
	the same condition. Both now build a `Values`, so there is one vocabulary and one resolver.

	Two layers, asked in this order:

	  * BUCKETS — what a writer put there, keyed by writer (`{"call_api_1": {...}, "_engine": {...}}`).
	    This is what persists between segments.
	  * RECORDS — the reachable documents, keyed by slug, each a zero-argument loader. Loaded at most
	    ONCE, on first reference, and never copied into the buckets. That is why a 30-day Wait reads the
	    lead as it is now rather than as it was when the journey started.

	It is a real mapping (`__getitem__`, `get`, `__contains__`) because an author's expression reads
	`ctx["crm_lead.date_2"]` through `frappe.safe_eval`, and a plain dict of namespaced keys cannot serve
	a value that lives on a live document.
	"""

	def __init__(self, buckets=None, records=None):
		self.buckets = {k: dict(v) for k, v in (buckets or {}).items() if isinstance(v, dict)}
		self._loaders = dict(records or {})
		self._loaded = {}

	# --- reading ------------------------------------------------------------------------------------

	def __getitem__(self, ref):
		found, value = self._lookup(ref)
		if not found:
			raise UnknownReference(
				f"{ref!r} is not a value this workflow can read — nothing before it produces it and "
				f"no record it can reach has it"
			)
		return value

	def get(self, ref, default=None):
		"""Mapping semantics, for a caller that has ALREADY established the reference exists (a predicate
		checks membership first, and raises its own authoring error when it does not). Every caller that
		must not silently succeed goes through `resolve`/`__getitem__` instead."""
		found, value = self._lookup(ref)
		return value if found else default

	def __contains__(self, ref):
		return self._lookup(ref)[0]

	def _lookup(self, ref):
		"""`(found, value)`. Node bucket first, then the reachable record — see the module docstring."""
		parsed = parse(ref)
		if parsed is None:
			return False, None
		source, field = parsed
		bucket = self.buckets.get(source)
		if bucket is not None and field in bucket:
			return True, bucket[field]
		record = self._record(source)
		if record is not None and field in record:
			return True, record[field]
		return False, None

	def _record(self, source):
		"""The reachable record for a slug, loaded at most once. A loader that cannot answer (a subject
		deleted mid-flight) yields an empty record, so state reads stay honest and the journey fails at its
		next REAL read rather than while assembling state."""
		if source in self._loaded:
			return self._loaded[source]
		loader = self._loaders.get(source)
		if loader is None:
			return None
		self._loaded[source] = loader() or {}
		return self._loaded[source]

	def keys(self):
		"""Every reference that currently answers. Records already loaded are included; an unloaded one is
		not walked, because enumerating state must never trigger a document read."""
		found = [of_node(source, field) for source, bucket in self.buckets.items() for field in bucket]
		for slug_, record in self._loaded.items():
			found += [f"{slug_}{SEP}{field}" for field in record]
		return found

	def __iter__(self):
		return iter(self.keys())

	def __len__(self):
		return len(self.keys())

	def __repr__(self):
		return f"Values(buckets={self.buckets!r}, records={sorted(self._loaders)})"

	# --- writing ------------------------------------------------------------------------------------

	def __setitem__(self, ref, value):
		"""Write a NAMESPACED reference. A bare name has no writer and is refused: every value must say
		where it came from, and the engine will not invent an owner for one that does not."""
		parsed = parse(ref)
		if parsed is None:
			raise UnknownReference(f"{ref!r} cannot be written — a value must say which node produced it")
		source, field = parsed
		self.buckets.setdefault(source, {})[field] = value

	def update(self, values):
		"""Write a bag of NAMESPACED references. Only the engine writes this way — a verb handler writes
		through `writing_as`, where a bare name is its own."""
		for key, value in (values or {}).items():
			self[key] = value

	def setdefault(self, ref, default=None):
		found, value = self._lookup(ref)
		if found:
			return value
		self[ref] = default
		return default

	def pop(self, ref, default=None):
		"""Take a value OUT of its writer's bucket. Only the engine does this — a verb names the edge it
		leaves by at `_engine.output`, and the interpreter consumes it so the next node cannot inherit it."""
		parsed = parse(ref)
		if parsed is None:
			return default
		source, field = parsed
		return self.buckets.get(source, {}).pop(field, default)

	def writing_as(self, node_id):
		"""A view in which THIS node is the writer, so a handler writes `assigned_to` and it lands at
		`<node_id>.assigned_to`.

		A verb handler does not and must not know its node id — it is one verb, reused by the rule lane
		and by two interpreter paths. The interpreter knows which node is running, so scoping belongs
		here. Reads through the view are the full namespaced vocabulary, unchanged.
		"""
		return _WriterView(self, node_id)


class _WriterView:
	"""One node's write scope over a shared `Values`. Reads pass straight through."""

	def __init__(self, values, node_id):
		self._values = values
		self._node_id = node_id

	@property
	def writer_id(self):
		"""Which node is writing. A handler that must NAMESPACE something of its own — a Call API judging
		`success_when` against its own response — asks the view rather than being told its node id twice."""
		return self._node_id

	def __getitem__(self, ref):
		return self._values[ref]

	def get(self, ref, default=None):
		return self._values.get(ref, default)

	def __contains__(self, ref):
		return ref in self._values

	def __iter__(self):
		return iter(self._values)

	def keys(self):
		return self._values.keys()

	def __setitem__(self, ref, value):
		"""A namespaced reference is honoured as written (that is how a handler reaches `_engine.output`);
		a bare name is this node's own value."""
		if parse(ref) is None:
			self._values.buckets.setdefault(self._node_id, {})[ref] = value
		else:
			self._values[ref] = value

	def update(self, values):
		for key, value in (values or {}).items():
			self[key] = value

	def pop(self, ref, default=None):
		return self._values.pop(ref, default)

	def writing_as(self, node_id):
		return _WriterView(self._values, node_id)
