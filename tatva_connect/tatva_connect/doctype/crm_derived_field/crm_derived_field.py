# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""An operator's own derived field — a list column computed from a doctype's columns and never stored.

There is no parser here and no expression language, because frappe already ships one: what is typed into
`buckets` is byte for byte what `frappe.get_list(filters=...)` is handed and what `evaluate_filters`
reads back. `derived.from_row` is the ONE reader of a stored row, so what this controller proves at Save
is exactly what the loader will serve; nothing about a declaration is decided twice.

REFUSAL AT SAVE IS THE WHOLE SAFETY STORY. A declaration whose buckets overlap, or whose display and
filter name different rows, fails SILENTLY at read time — a record shows in a bucket the filter will
never return, and falls out of every board column. So `derived.verify()` runs on every save: it builds a
corpus out of the declaration's own operands, puts a probe row on every boundary they name, asserts both
of frappe's readers agree, and rolls back. Nothing it inserts survives the call, and no probe row is ever
deleted — `delete_doc` enqueues background work and fires doc_events, and a rollback touches neither.

ONE registry, two sources. `list_engine/fields.py` holds the declarations that ship with the code and
this doctype holds the operator's; both land in the same registry and nothing downstream — the engine,
the five menus, the renderers, the wire contract — has ever known the difference. Code wins the merge, so
a row shadowing a code declaration would be stored, enabled and then silently never served: that
collision is refused here, which is the only place it can be refused at all.

The shape checks below are the AUTHOR-time gate on the raw JSON, and they exist for the cases
`derived.from_row` reads leniently on purpose — a bucket that is not an object, a misspelt key, a missing
value. Everything a declaration MEANS is still decided by `_validate` and `verify()` and never restated.

Plan: docs/plans/tasks-ui/2026-07-31-derived-field-head.md
"""

import itertools
import json

import frappe
from frappe import _
from frappe.model import default_fields
from frappe.model.document import Document
from frappe.utils import escape_html, now, nowdate
from frappe.utils.data import evaluate_filters

from tatva_connect.list_engine import derived, repair

# What one bucket may declare. Anything else is a misspelling `from_row` would read past in silence.
_BUCKET_KEYS = frozenset({"value", "theme", "filters"})

# The mandatory columns a probe row does not get from the declaration are filled by fieldtype, generically.
_PROBE_TEXT = "Derived Field Probe"
_PROBE_TEXTUAL = frozenset({"Data", "Small Text", "Text", "Long Text", "Text Editor", "Code", "HTML Editor"})
_PROBE_NUMERIC = frozenset({"Int", "Float", "Currency", "Percent", "Check", "Rating"})

# One refusal names every kind of problem, but one corpus can raise hundreds of a kind — this is enough to act on.
_MAX_REPORTED = 5

# What each way of failing means, and the edit that fixes it. Keyed by `Disagreement.kind`.
_KINDS = {
	"overlapping-buckets": (
		"Two buckets claim the same record",
		"A record can only read as one value, and SQL has no first-match rule to break the tie — so the "
		"column would show one value while a filter on it returned the other. Narrow one of the two. A range "
		"is half-open: <b>&gt;=</b> the start and <b>&lt;</b> the end, never <b>&lt;=</b> on both sides.",
	),
	"reader-disagreement": (
		"The records this bucket shows are not the records it returns",
		"The list and a filter on this bucket name different records. This is the silent failure the check "
		"exists to catch. Look for a bound two buckets can both claim, or a comparison the column and the "
		"list read differently; the bucket named above is the one whose records do not match.",
	),
	"reader-error": (
		"This bucket's filters could not be read",
		"The operator or the value is not one this column accepts. Check the fieldname, the operator and the "
		"type of the value against the column it names.",
	),
	"operand-unrepresentable": (
		"A value the buckets compare against is one the column cannot hold",
		"No record could be put on the boundary that value names, so that part of the declaration is "
		"unproven and must not be trusted. Compare the column against a value of its own type.",
	),
	"corpus-unrepresentable": (
		"A trial record could not be created",
		"Part of the declaration went unproven because the list's own validation refused the trial record. "
		"The reason is quoted above; a mandatory column with no default is the usual one.",
	),
	"corpus-capped": (
		"The declaration reads too many columns to prove exhaustively",
		"Only the first combinations were checked, so the rest of the declaration is unproven. Build the "
		"field out of fewer columns.",
	),
}


class CRMDerivedField(Document):
	"""One operator-authored derived field. Proved on real records before it is allowed to exist."""

	def validate(self):
		self.fieldname = (self.fieldname or "").strip()
		self.surfaces = ", ".join(derived._surfaces(self.surfaces))
		# A blank required field is the framework's own message to give; ours would only obscure it.
		if not (self.dt and self.fieldname and self.label and self.buckets):
			return
		self._assert_addressable()
		self._assert_buckets_shape()
		field = self._declaration()
		self._assert_free(field)
		# NO PROOF RUNS HERE. `verify()` used to insert a boundary corpus of real records on every save to
		# prove the two readers agree. That is a DEVELOPER's check and it belongs in tests/list_engine,
		# where it still runs against a controlled fixture — not in a controller, where it wrote sixty
		# trial rows into whatever database the save landed in and could refuse a deployment for a reason
		# that has nothing to do with the declaration: on UAT it borrowed a `Lost` CRM Lead Status, crm's
		# own validate_lost_reason demanded `lost_reason`, and seed 49 died. Everything above is metadata
		# only — it reads `get_meta` and writes nothing.

	def _being_retired(self):
		"""Whether THIS save is the enabled switch going off. Not "is off" — a row that was already off and
		is being edited is a draft, and a draft that cannot be served must still be refused."""
		before = self.get_doc_before_save()
		return bool(before and before.enabled and not self.enabled)

	# Dropping the cache IS publishing the edit; `declaration_version()` moves with it.
	def on_update(self):
		derived.reload()

	def on_trash(self):
		derived.reload()

	def _assert_addressable(self):
		"""A fieldname is a key every surface addresses — the column header, the sort, the board, the export."""
		if self.fieldname != frappe.scrub(self.fieldname) or not self.fieldname.isidentifier():
			frappe.throw(
				_(
					"{0} is not a fieldname. Use lower case letters, digits and underscores, as in {1}."
				).format(frappe.bold(escape_html(self.fieldname)), frappe.bold("hba1c_band")),
				title=_("That is not a fieldname"),
			)
		meta = frappe.get_meta(self.dt)
		# A Single holds one record and a child table has no list of its own, so neither has a column to carry.
		if meta.issingle or meta.istable:
			frappe.throw(
				_("{0} has no list for a column to appear on, so it cannot carry a derived field.").format(
					frappe.bold(self.dt)
				),
				title=_("That list does not exist"),
			)

	def _assert_buckets_shape(self):
		"""The raw JSON, checked where `from_row` reads leniently — a bucket it would drop or read as blank.

		Everything else about the buckets is `_validate`'s and `verify()`'s, and is not restated here."""
		try:
			declared = json.loads(self.buckets)
		except (TypeError, ValueError) as unreadable:
			frappe.throw(
				_("Buckets is not valid JSON: {0}").format(escape_html(str(unreadable))),
				title=_("That JSON cannot be read"),
			)
		if not isinstance(declared, list):
			frappe.throw(
				_(
					"Buckets is an ordered LIST of buckets, not {0}. The order decides both the value a record shows and the order the field sorts in."
				).format(frappe.bold(type(declared).__name__)),
				title=_("Buckets is a list"),
			)
		for bucket in declared:
			self._assert_bucket_shape(bucket)

	def _assert_bucket_shape(self, bucket):
		if not isinstance(bucket, dict):
			frappe.throw(
				_("{0} is not a bucket. A bucket is an object with a value and its filters.").format(
					frappe.bold(escape_html(str(bucket)))
				),
				title=_("That is not a bucket"),
			)
		unknown = sorted(set(bucket) - _BUCKET_KEYS)
		if unknown:
			frappe.throw(
				_(
					"A bucket declares value, theme and filters — not {0}. A key nothing reads would be saved and then ignored."
				).format(frappe.bold(escape_html(", ".join(unknown)))),
				title=_("That key is read by nobody"),
			)
		if not isinstance(bucket.get("value"), str) or not bucket["value"].strip():
			frappe.throw(
				_("Every bucket needs a value the rep reads. {0} has none.").format(
					frappe.bold(escape_html(json.dumps(bucket)))
				),
				title=_("A bucket has no value"),
			)
		# A value is a Select option, and options travel newline-separated: one line break inside a value
		# splits it into two phantom entries in every menu, neither of which any record can ever read as.
		if bucket["value"] != bucket["value"].strip() or any(c in bucket["value"] for c in "\n\r\t"):
			frappe.throw(
				_("{0} has a line break or padding around it. A value is one line of text.").format(
					frappe.bold(escape_html(repr(bucket["value"])))
				),
				title=_("That value is not one line"),
			)
		if bucket.get("theme") is not None and not isinstance(bucket["theme"], str):
			frappe.throw(
				_("{0}: theme is the name of a colour, as text.").format(frappe.bold(bucket["value"])),
				title=_("That theme is not a colour"),
			)
		if not isinstance(bucket.get("filters"), list):
			frappe.throw(
				_("{0}: filters is a list of [fieldname, operator, value].").format(
					frappe.bold(bucket["value"])
				),
				title=_("A bucket has no filters"),
			)
		for term in bucket["filters"]:
			if not isinstance(term, list | tuple):
				frappe.throw(
					_("{0}: a filter is [fieldname, operator, value], not {1}.").format(
						frappe.bold(bucket["value"]), frappe.bold(escape_html(json.dumps(term)))
					),
					title=_("That is not a filter"),
				)

	def _declaration(self):
		"""The row read as a declaration, through the ONE reader the loader uses. Shape is checked in there;
		whether the declaration MEANS one thing is `_prove`."""
		try:
			return derived.from_row(self)
		except derived.DerivedFieldError as malformed:
			frappe.throw(escape_html(str(malformed)), title=_("The declaration is not well formed"))

	def _assert_free(self, field):
		"""The name has to be the list's to give, and the tiebreaker has to be a column it really has.

		The first two rules are also `derived._assert_declarable`'s, held over the LIVE registry at request
		time — asked here so a collision is refused at Save instead of taking every list of that doctype
		down. The code collision has to be refused here and nowhere else: code wins the merge, so a row that
		shadowed `fields.py` would save, enable, and then never be served by anything."""
		meta = frappe.get_meta(self.dt)
		if meta.get_field(self.fieldname) or self.fieldname in default_fields:
			frappe.throw(
				_(
					"{0} already has a column called {1}. A derived field of that name would make the cell and a filter on it disagree, so every {0} list would be unserveable."
				).format(frappe.bold(self.dt), frappe.bold(self.fieldname)),
				title=_("That name is taken"),
			)
		if derived.code_declared(self.dt, self.fieldname):
			frappe.throw(
				_(
					"{0} is already declared for {1} in the code, in list_engine/fields.py. Code wins, so this row would be stored and then never served — retire the code declaration, or choose another name."
				).format(frappe.bold(self.fieldname), frappe.bold(self.dt)),
				title=_("That field is already declared"),
			)
		# A bucket reads real columns. Unchecked, a typo reached SQL and answered the operator a raw
		# `OperationalError: Unknown column`, which is not a refusal anyone can act on.
		unknown = [
			source
			for source in field.depends_on
			if source not in default_fields and not derived.fieldtype_of(self.dt, source)
		]
		if unknown:
			frappe.throw(
				_("{0} has no column called {1}. A bucket can only read columns the list really has.").format(
					frappe.bold(self.dt), frappe.bold(escape_html(", ".join(sorted(unknown))))
				),
				title=_("That column does not exist"),
			)
		if field.order_by and not (
			field.order_by in default_fields or derived.fieldtype_of(self.dt, field.order_by)
		):
			frappe.throw(
				_("{0} is not a column on {1}, so it cannot order the rows inside a bucket.").format(
					frappe.bold(field.order_by), frappe.bold(self.dt)
				),
				title=_("That column does not exist"),
			)

	def _prove(self, field):
		"""The declaration, put on every boundary it names, on real records, and rolled back."""
		snap = derived.snapshot()
		problems = derived.verify(field, defaults=_probe_defaults(self.dt), snap=snap)
		if problems:
			_refuse(field, problems, snap)


@frappe.whitelist()
def usage(dt: str, fieldname: str):
	"""How many saved views name this field, and how many people they belong to.

	The form asks before an operator retires or deletes a declaration, so the consequence is a number on
	screen rather than something discovered by a rep the next morning. Read-only, and the count is
	`repair`'s — the same function that does the cleanup, so the warning cannot promise something the
	janitor does not do."""
	frappe.only_for("System Manager")
	return repair.views_using(dt, fieldname)


def _probe_defaults(doctype):
	"""A value for every mandatory column of the list, so `verify()` can insert its probe rows.

	A value is COPIED off a record the list already holds, because a stored record has by definition passed
	every rule the doctype enforces — including the conditional ones no meta can describe. Inventing one
	instead is what broke a UAT seed: the probe borrowed the first `CRM Lead Status` it found, that site's
	happened to be a `Lost` one, and crm's own `validate_lost_reason` then demanded `lost_reason` — a column
	that is not `reqd`, carries no `mandatory_depends_on`, and is enforced in Python where nothing generic
	can see it. Every one of the sixty trial rows failed and the declaration was refused.

	Falling back to a fieldtype guess only when the list is EMPTY, where there is no rule to trip over.
	A column neither route can fill is left out, and `verify()` then reports the trial rows it could not
	create rather than passing over them in silence."""
	defaults = {}
	for df in frappe.get_meta(doctype).fields:
		if not df.reqd or df.default:
			continue
		value = frappe.db.get_value(doctype, {df.fieldname: ["is", "set"]}, df.fieldname)
		if value is None:
			value = _probe_value(df)
		if value is not None:
			defaults[df.fieldname] = value
	return defaults


def _probe_value(df):
	if df.fieldtype in _PROBE_TEXTUAL:
		return _PROBE_TEXT
	if df.fieldtype in _PROBE_NUMERIC:
		return 0
	if df.fieldtype == "Select":
		return next((option for option in (df.options or "").split("\n") if option.strip()), None)
	if df.fieldtype == "Date":
		return nowdate()
	if df.fieldtype == "Datetime":
		return now()
	if df.fieldtype == "Time":
		return "00:00:00"
	if df.fieldtype == "Link" and df.options:
		return frappe.db.get_value(df.options, {}, "name")
	return None


def _refuse(field, problems, snap):
	"""One refusal listing every way the declaration fails, in the operator's own terms.

	`verify()` names the probe ROW an overlap was found on, and that row is rolled back before this is
	reached, so its name means nothing to whoever has to fix the JSON. The values are recovered here off
	the same corpus, so the message says which buckets collide and on what."""
	grouped = {}
	for problem in problems:
		grouped.setdefault(problem.kind, []).append(problem)

	collisions = _collisions(field, snap) if "overlapping-buckets" in grouped else {}
	lines = []
	for kind, group in grouped.items():
		heading, explanation = _KINDS.get(kind, (kind, ""))
		lines.append(f"<b>{heading}</b>")
		lines.extend(_details(kind, group, collisions))
		lines.append(explanation)
		lines.append("")
	frappe.throw("<br>".join(lines), title=_("These buckets do not mean one thing"))


def _details(kind, group, collisions):
	"""The specific instances of one kind of failure, bounded, with a count of what is not shown."""
	if kind == "overlapping-buckets" and collisions:
		shown = [
			_("{0} and {1} both claim a record where {2}.").format(
				frappe.bold(escape_html(pair[0])), frappe.bold(escape_html(pair[1])), _values(values)
			)
			for pair, values in list(collisions.items())[:_MAX_REPORTED]
		]
		hidden = len(collisions) - len(shown)
	else:
		shown = [
			(f"{frappe.bold(escape_html(str(problem.bucket)))}: " if problem.bucket else "")
			+ escape_html(str(problem.detail))
			for problem in group[:_MAX_REPORTED]
		]
		hidden = len(group) - len(shown)
	if hidden > 0:
		shown.append(_("… and {0} more.").format(hidden))
	return shown


def _values(values):
	"""One probe record as an operator reads it — an empty column is empty, not None."""
	return ", ".join(
		f"{frappe.bold(escape_html(column))} is "
		f"{escape_html(str(value)) if value is not None else _('empty')}"
		for column, value in values.items()
	)


def _collisions(field, snap):
	"""Every pair of buckets that claim one record, and the values proving it, over the same corpus
	`verify()` built. In memory and read-only: this decides nothing, it only explains what verify() found."""
	found = {}
	combinations, _wanted = derived._corpus(field, snap)
	for values in combinations:
		row = frappe._dict(values)
		claimed = [
			bucket.value
			for bucket in field.buckets
			if evaluate_filters(row, derived.resolve(field, bucket, snap))
		]
		for pair in itertools.combinations(claimed, 2):
			found.setdefault(pair, values)
	return found
