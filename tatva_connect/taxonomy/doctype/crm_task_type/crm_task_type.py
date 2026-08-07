# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model import NO_VALUE_FIELDS
from frappe.model.document import Document
from frappe.utils import cstr

from tatva_connect.taxonomy.normalize import normalize_field


def _options_of(row):
	"""The choices a declared field offers, as a list. ONE reading of `options`, because a rule's value and
	the location condition's value are both checked against it and two readings could disagree on trimming
	or on a trailing blank line."""
	return [o.strip() for o in (row.options or "").split("\n") if o.strip()]


class CRMTaskType(Document):
	def validate(self):
		# M-2: normalize the display value so "Apollo " / "apollo" never fork.
		normalize_field(self, "type_name")
		self._validate_schema()
		self._validate_lead_sourced_fields()
		self._validate_rules()
		self._validate_location_condition()
		self._validate_link_fields_name_a_doctype()

	def _validate_schema(self):
		"""A row that asks the rep something must say what it asks. A LAYOUT row (`NO_VALUE_FIELDS` — Frappe's
		own list, the same one that keeps a Section Break out of a table's columns) stores nothing, so a Column
		Break carrying no heading is correct rather than incomplete."""
		for row in self.schema:
			if (row.fieldtype or "") not in NO_VALUE_FIELDS and not (row.label or "").strip():
				frappe.throw(_("Schema row {0}: a {1} field needs a label — it is what the rep is asked.").format(
					row.idx, row.fieldtype), title=_("Missing label"))

	def _validate_lead_sourced_fields(self):
		"""A `source = Lead` row IS the lead's field, so it must carry the LEAD's fieldname.

		The prefill looks the row's `fieldname` up through the lead detail brain and the snapshot stores it
		under that same name, so a row calling the lead's `first_name` something else — `patient_name`, say —
		opens blank and snapshots under a name nothing answers to: two names for one thing.

		Asked of `CRM Lead API Field`, the lead resource's own field brain, and NOT of `CRM Lead`'s meta:
		only 59 of its 315 rows are columns of CRM Lead itself, the other 256 living on the child profiles
		(drug, care, plan, lab, acq, metrics). Asking the meta refused four lead fields out of five —
		the oncologist, the cancer stage, the hospital — which are exactly the context a snapshot exists for.
		`lead.detail` already reads every section, so the prefill was never the thing that was narrow."""
		from tatva_connect.activity.api import LEAD_SOURCE

		catalogued = {r.fieldname for r in frappe.get_all("CRM Lead API Field", fields=["fieldname"], limit=0)}
		lead_meta = frappe.get_meta("CRM Lead")
		for row in self.schema:
			fieldname = (row.fieldname or "").strip()
			if (row.get("source") or "") != LEAD_SOURCE or not fieldname:
				continue
			# Either door proves it is the lead's: a catalogued field (which may live on a child profile) or a plain column of CRM Lead itself.
			if fieldname not in catalogued and not lead_meta.get_field(fieldname):
				frappe.throw(
					_("Schema row {0}: `{1}` is sourced from the Lead but is neither a catalogued lead field "
					  "nor a column of CRM Lead. A lead-sourced row must carry the lead's own fieldname, or it "
					  "opens blank and snapshots under a name nothing answers to.").format(row.idx, fieldname),
					title=_("Not a lead field"))

	def _validate_rules(self):
		"""Every rule row names fields THIS type declares, and a value the named field offers (§17.1).

		A rule is the form's behaviour, so a row naming a field that does not exist is a reaction that can
		never fire pointed at a target that can never be reached — silent on screen and impossible to find by
		reading a grid of nineteen rows. It is refused here with its ROW NUMBER, which is the only address an
		admin has for a child row.

		The declaration is the enforcement: the offered values are the field's own `options`, and the operator
		vocabulary is the compile's (`activity.api.RULE_VALUE_OPERATORS`) rather than restated here."""
		from tatva_connect.activity.api import (
			RULE_SET_VALUE,
			RULE_VALUE_OPERATORS,
			rule_conditions,
			rule_targets,
		)

		rows, questions = self._declared_rows(), self._declared_questions()
		for row in self.rules:
			# Every triplet the compile reads is judged here — asked of `rule_conditions` so neither can see more of a row than the other.
			for field, operator, value in rule_conditions(row):
				self._validate_condition(row, field, operator, value, rows, questions)
			if (row.action or "") == RULE_SET_VALUE:
				self._validate_copy_source(row, questions)
			for target in rule_targets(row.targets):
				if target not in rows:
					frappe.throw(
						_("Rule row {0}: {1} is not a field this task type declares.").format(row.idx, target),
						title=_("Unknown target"))
		self._validate_copy_graph()

	def _validate_copy_graph(self):
		"""Set Value copies must not run in a circle, across rows as well as within one.

		A cycle has no fixpoint, so nothing downstream can settle it: `copied_values` swaps the pair and
		stops on whichever parity its bound lands on, which makes a no-op re-save mutate the record — and
		the browser, which re-runs on its own reactivity, never converges at all. Refused here because a
		graph is a property of the whole rule set and no single row can see it."""
		from tatva_connect.activity.api import RULE_SET_VALUE, rule_targets

		edges = {}
		for row in self.rules:
			if (row.action or "") != RULE_SET_VALUE:
				continue
			source = cstr(row.get("set_value") or "").strip()
			for target in rule_targets(row.targets):
				edges.setdefault(source, set()).add(target)
		seen, path = set(), []

		def walk(node):
			if node in path:
				frappe.throw(
					_("Set Value copies in a circle: {0}.").format(" → ".join([*path[path.index(node):], node])),
					title=_("Circular copy"))
			if node in seen:
				return
			seen.add(node)
			path.append(node)
			for nxt in edges.get(node, ()):
				walk(nxt)
			path.pop()

		for source in list(edges):
			walk(source)

	def _validate_copy_source(self, row, questions):
		"""A Set Value row copies one declared field into another, so its source must be a field that holds
		an answer — and never the target itself, which would copy a field onto itself and read as working.

		Checked here for the same reason a When value is: the declaration is the enforcement, and raw SQL
		aside, a source naming nothing is a rule that fires and copies blank over whatever the field held."""
		from tatva_connect.activity.api import LEAD_SOURCE, rule_targets

		source = cstr(row.get("set_value") or "").strip()
		if not source:
			frappe.throw(
				_("Rule row {0}: Set Value names {1} but declares no field to copy from.").format(
					row.idx, row.targets),
				title=_("Set Value needs a source"))
		if source not in questions:
			frappe.throw(
				_("Rule row {0}: {1} is not a field this task type declares an answer for, so there is "
				  "nothing to copy from it.").format(row.idx, source),
				title=_("Unknown source"))
		for target in rule_targets(row.targets):
			if target == source:
				frappe.throw(
					_("Rule row {0}: Set Value copies {1} onto itself.").format(row.idx, source),
					title=_("Copies itself"))
			# A layout row is a legitimate target for Show and Hide and holds nothing to write, so a copy
			# aimed at one reads as configured in the grid and silently does nothing.
			if target not in questions:
				frappe.throw(
					_("Rule row {0}: {1} holds no value, so Set Value has nowhere to copy into.").format(
						row.idx, target),
					title=_("Not a question"))
			# The lead answers a lead-sourced field, so a copy would show one value read-only and store another.
			if (questions[target].get("source") or "") == LEAD_SOURCE:
				frappe.throw(
					_("Rule row {0}: {1} is answered by the lead, so Set Value cannot fill it.").format(
						row.idx, target),
					title=_("Answered by the lead"))

	def _validate_condition(self, row, field, operator, value, rows, questions):
		"""ONE When triplet: it names a field this type declares, that field holds an answer, and the value
		is one the field offers. Asked of both triplets so neither can be checked more loosely than the other."""
		from tatva_connect.activity.api import RULE_VALUE_OPERATORS

		field = (field or "").strip()
		if not field:
			return
		if field not in rows:
			frappe.throw(_("Rule row {0}: {1} is not a field this task type declares.").format(row.idx, field),
						 title=_("Unknown field"))
		# A layout row holds no answer to read, so such a rule looks right in the grid and never fires; as a TARGET it is fine, that is how a section hides.
		if field not in questions:
			frappe.throw(_("Rule row {0}: {1} is a layout row and holds no value to test.").format(row.idx, field),
						 title=_("Not a question"))
		value = cstr(value or "").strip()
		if value and (operator or "").strip() in RULE_VALUE_OPERATORS:
			options = _options_of(questions[field])
			if options and value not in options:
				frappe.throw(
					_("Rule row {0}: {1} is not one of the options {2} declares.").format(row.idx, value, field),
					title=_("Unknown value"))

	def _declared_rows(self):
		"""Every declared row of this type, keyed by fieldname — layout markers INCLUDED.

		This is what a rule may TARGET: a Section Break is a legitimate target, because showing or hiding one
		is how a whole section appears. The two helpers here are the only definitions of "what this type
		declares" on this doctype, so no validator builds its own."""
		return {(f.fieldname or "").strip(): f for f in self.schema if (f.fieldname or "").strip()}

	def _declared_questions(self):
		"""The declared rows that hold an ANSWER — layout markers excluded, because they store nothing.

		This is what any PREDICATE may name, whether it is a rule's When field or the location condition.
		Both ask this one helper, so the two can never disagree about what is askable."""
		return {name: f for name, f in self._declared_rows().items()
				if (f.fieldtype or "") not in NO_VALUE_FIELDS}

	def _validate_location_condition(self):
		"""The location gate's condition must name a field THIS type declares, and a value that field offers.

		A predicate over answers the form does not collect is pointless: it can never hold, so the gate can
		never fire, and the type reads as location-guarded while guarding nothing. The condition is evaluated
		against this type's own settled answers (`location.api._condition_holds`), so the set a condition may
		name is exactly the set declared under Fields — the same relationship `_validate_rules` enforces for a
		rule's When field, checked here with the same two questions so the two cannot drift.

		Its own value list is skipped for `is set` / `is not set`, which ask only whether an answer exists —
		the same rule the compile applies (`RULE_VALUE_OPERATORS`)."""
		from tatva_connect.activity.api import RULE_VALUE_OPERATORS

		field = (self.get("location_condition_field") or "").strip()
		if not field:
			return  # no condition declared: visit_mode alone decides, and that is a complete declaration
		questions = self._declared_questions()
		if field not in questions:
			frappe.throw(
				_("Location Required When names `{0}`, which is not a question this task type asks. A location "
				  "condition can only be asked of a field declared under Fields — otherwise it can never hold "
				  "and the location is never demanded.").format(field),
				title=_("Not a declared field"))
		value = cstr(self.get("location_condition_value") or "").strip()
		if value and (self.get("location_operator") or "").strip() in RULE_VALUE_OPERATORS:
			options = _options_of(questions[field])
			if options and value not in options:
				frappe.throw(
					_("Location Required When compares `{0}` against `{1}`, which is not one of the options "
					  "`{0}` declares.").format(field, value),
					title=_("Unknown value"))

	def _validate_link_fields_name_a_doctype(self):
		"""A `Link` row must say WHICH doctype it links to, or the rep is handed a picker over nothing.

		`options` is free text because a `Select` row uses it for its newline-separated choices, so nothing
		ever checked the `Link` case. Two different failures, and they are refused differently:

		* **Naming a doctype that does not exist** is unambiguously an error, so it is refused outright.
		* **Naming nothing** is refused only on a row that is NEW or whose fieldtype/options just changed.
		  14 of the 66 seeded types carry such a row already (`Select ASM`, `Select CS Agent`, the three
		  `Select Junior Coach *`), and refusing them outright would make those types unsaveable — an
		  operator editing an unrelated field would be blocked by a defect they did not introduce and cannot
		  safely fix, because what those fields actually hold is unknown: if the migration writes a plain
		  name rather than a User id, declaring them `Link → User` would refuse the migrated value. So the
		  rule hardens every new declaration and leaves the existing ones to be corrected deliberately.

		This is D-O's lesson applied again: scope a new rule to the TRANSITION and the old state keeps
		working. `has_value_changed` is not available on a child row, so the before-image is read off
		`get_doc_before_save()` by row name — absent for a new row, which is exactly the case to judge.

		Deliberately a refusal and not a picker: the choice is one of a thousand doctypes, and a dropdown
		that long teaches nothing. A named refusal at authoring time does."""
		before = {r.name: r for r in (getattr(self.get_doc_before_save(), "schema", None) or [])}
		for row in self.schema:
			if (row.fieldtype or "") != "Link":
				continue
			target = (row.options or "").strip()
			if target and not frappe.db.exists("DocType", target):
				frappe.throw(
					_("Schema row {0}: `{1}` links to `{2}`, which is not a DocType on this site.").format(
						row.idx, row.label or row.fieldname, target),
					title=_("Unknown DocType"))
			if target:
				continue
			was = before.get(row.name)
			if was and (was.fieldtype or "") == "Link" and not (was.options or "").strip():
				continue  # already in this state before the save: a pre-existing defect, not this edit's
			frappe.throw(
				_("Schema row {0}: `{1}` is a Link but names no DocType in Options, so the rep would be "
				  "offered a picker over nothing.").format(row.idx, row.label or row.fieldname),
				title=_("Link needs a target"))


@frappe.whitelist()
def list_target_columns(section=None):
	"""The columns a schema row's Target may name, as [{fieldname, label}] — the picker behind a free-text
	box that nothing checked.

	It asks `field_target`'s OWN inputs and holds no list of its own:

	* a row naming a SECTION may target a column of that section's `target_doctype` — read off the
	  `CRM Task Section` row, which is the one declaration of whose columns those are (rule 1);
	* a row naming NO section may target a settable column of `CRM Task` — read through
	  `activity.api.task_columns()`, the Task resource's own field brain, which is the ONE gate on that
	  question and is request-cached (rule 2).

	So the list offered is exactly the set the router will honour, and a value picked from it cannot become
	the silent misroute that put 45 dead targets on the live seed. Blank in either case stays legitimate — an
	untargeted row is addressed by its own fieldname (rules 3 and 4) and needs no pick.

	Gated on read of the doctype this picker paints; layout markers are excluded by the caller, not here."""
	from tatva_connect.activity.api import task_columns

	frappe.has_permission("CRM Task Type", "read", throw=True)

	section = (section or "").strip()
	if not section:
		# `task_columns()` is the GATE and answers in fieldnames; the label lives on the same `CRM Task Field`
		# row and is read for presentation only. Without it this branch offered `custom_followup_at` while the
		# section branch offered "Follow-up At" — one picker reading two ways.
		settable = task_columns()
		labels = dict(frappe.get_all(
			"CRM Task Field", filters={"fieldname": ["in", settable]},
			fields=["fieldname", "label"], as_list=True, limit=0))
		return [{"fieldname": c, "label": labels.get(c) or c} for c in settable]
	target_doctype = frappe.get_cached_value("CRM Task Section", section, "target_doctype")
	if not target_doctype:
		return []  # a section that is not declared owns no columns; the router falls back and says so
	return [{"fieldname": f.fieldname, "label": f.label or f.fieldname}
			for f in frappe.get_meta(target_doctype).fields
			if f.fieldtype not in NO_VALUE_FIELDS]


@frappe.whitelist()
def list_lead_fields(vertical=None, group=None, program=None):
	"""The lead fields a schema row may source at this grain, as [{fieldname, label, fieldtype}] (D31).

	The catalogue is the ONE lead-field brain — `lead/mapping.py:mappable_fields`, which reads
	`CRM Lead API Field` and drops any row naming no live column. Nothing is re-decided here.

	It is NOT filtered by `automation/fields.py:is_settable` any more. That gate asked *"may an automation
	WRITE this field"*, and it was right while the form wrote lead answers back to the lead. §4.2 of the
	generic-activity-storage plan reversed that: a lead field is now shown read-only and snapshotted onto
	the activity, so a write allowlist is the wrong question — and the wrong ANSWER, because the very
	fields a snapshot exists for (the patient's name, the oncologist, the stage) are the ones no contract
	ticks settable. Reading is still gated where it always was: `activity.api.lead_field_values` goes
	through the lead detail brain, so a field this viewer may not see never reaches them.
	The axes come from the CALLER (the open, possibly unsaved Desk form), the same shape as
	`intake/api.py:list_target_fields`. Gated read-only on the doctype this form edits."""
	from tatva_connect.lead import mapping

	frappe.has_permission("CRM Task Type", "read", throw=True)

	axes = ((vertical or "").strip(), (group or "").strip(), (program or "").strip())
	if not any(axes):
		return []  # no grain chosen yet — the client shows "pick the grain first"
	return [{"fieldname": f["fieldname"], "label": f["label"], "fieldtype": f["fieldtype"]}
			for f in mapping.mappable_fields(grain=axes)]
