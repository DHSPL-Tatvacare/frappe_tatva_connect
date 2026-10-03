# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

import re

import frappe
from frappe import _
from frappe.model import NO_VALUE_FIELDS
from frappe.model.document import Document
from frappe.utils import cstr

from tatva_connect.authoring import lifecycle
from tatva_connect.authoring import versions as authoring_versions
from tatva_connect.integrity import field_usage
from tatva_connect.taxonomy import form_versions
from tatva_connect.taxonomy.normalize import normalize_field
from tatva_connect.workflow_engine import registry


def _options_of(row):
	"""The choices a declared field offers, as a list. ONE reading of `options`, because a rule's value and
	the location condition's value are both checked against it and two readings could disagree on trimming
	or on a trailing blank line."""
	return [o.strip() for o in (row.options or "").split("\n") if o.strip()]


def question_types():
	"""The types a question may take — the child doctype's own Field Type options, layout breaks excluded."""
	options = frappe.get_meta("CRM Task Type Field").get_field("fieldtype").options or ""
	return [t for t in options.split("\n") if t and t not in NO_VALUE_FIELDS]


# What a column can hold: its own type, or a text answer when it holds text — the shape of every seeded binding.
TEXT_COLUMNS = frozenset(("Data", "Small Text", "Text", "Long Text", "Text Editor"))
TEXT_ANSWERS = frozenset(("Data", "Small Text", "Select"))


def column_takes(column_type, question_type):
	return question_type == column_type or (column_type in TEXT_COLUMNS and question_type in TEXT_ANSWERS)


def _at(node_id, message, code, field=None, fix=None):
	"""One form fault as the workflow problem record (`registry.problem`), anchored on the question key or `rule:<idx>` it names."""
	return {"node_id": node_id, **registry.problem(message, field=field, code=code, fix=fix)}


def _rule(row):
	return f"rule:{row.idx}"


def _home(row):
	"""Where a question's answers are stored, as `field_target` reads it."""
	return (row.get("source") or ""), (row.get("section") or ""), (row.get("target") or "")


def _keys(form):
	"""The keys a form declares, layout rows included."""
	return {(f.fieldname or "").strip() for f in form.schema if (f.fieldname or "").strip()}


class CRMTaskType(Document):
	def validate(self):
		# M-2: normalize the display value so "Apollo " / "apollo" never fork.
		normalize_field(self, "type_name")
		changed = self._definition_changed()
		self._refuse_editing_a_released_form(changed)
		self._bind_lead_rows_to_snapshot()
		if changed:
			lifecycle.refuse(self.form_problems(registry.DRAFT), _("This form cannot be saved yet"))
		# A form never published is served as it stands, so a question it drops is judged at save; a published one is judged at Publish.
		if not form_versions.current_name(self.name):
			field_usage.guard_task_type(self)

	def _bind_lead_rows_to_snapshot(self):
		"""A lead question's value is snapshotted into the section declared to hold lead snapshots, whichever form wrote the row."""
		from tatva_connect.activity.api import LEAD_SOURCE

		section = frappe.db.get_value("CRM Task Section", {"is_lead_snapshot": 1})
		for row in self.schema:
			if section and (row.get("source") or "") == LEAD_SOURCE and not row.section:
				row.section = section

	def on_trash(self):
		field_usage.guard_task_type(self, deleting=True)

	def onload(self):
		"""Desk's lifecycle buttons: the legal moves from this state, each by its verb, so Desk offers and never decides."""
		self.set_onload("moves", lifecycle.moves(self.lifecycle_state))

	@staticmethod
	def default_list_data():
		"""Columns and fields the CRM list view opens with (Task Forms). Required by `crm.api.doc.get_data`."""
		# No `group` column: frappe-ui ListRow reads a truthy `row.group` as a group-by header and breaks row selection.
		columns = [
			{"label": "Task Type", "type": "Data", "key": "type_name", "width": "16rem"},
			{"label": "Status", "type": "Select", "key": "lifecycle_state", "width": "7rem"},
			{"label": "Vertical", "type": "Link", "options": "CRM Vertical", "key": "vertical", "width": "9rem"},
			{"label": "Program", "type": "Link", "options": "CRM Program", "key": "program", "width": "10rem"},
			{"label": "Visit Mode", "type": "Select", "key": "visit_mode", "width": "8rem"},
			{"label": "Last Modified", "type": "Datetime", "key": "modified", "width": "8rem"},
		]
		rows = ["name", "type_name", "lifecycle_state", "enabled", "vertical", "program", "visit_mode", "modified"]
		return {"columns": columns, "rows": rows}

	# --- the lifecycle: the shared state machine (`authoring.lifecycle`), asked exactly as a workflow asks it ---

	def is_editable(self) -> bool:
		"""Authoring is a DRAFT-only operation; reps keep the published version while a Draft is worked on."""
		return lifecycle.is_editable(self.lifecycle_state)

	def can_transition_to(self, target) -> bool:
		return lifecycle.can_move(self.lifecycle_state, target)

	def apply_transition(self, target):
		"""Move the lifecycle, or say why not. The ONE place a form's state changes; Publish checks and freezes first."""
		lifecycle.assert_move(self.lifecycle_state, target, _("form"))
		if target == lifecycle.PUBLISHED:
			self.assert_publishable()
			self.freeze_version()
		self.lifecycle_state = target
		# Reps are offered the form only while it is Active; Revise leaves this alone, so the published version keeps serving.
		if target == lifecycle.ACTIVE:
			self.enabled = 1
		elif target in (lifecycle.SUSPENDED, lifecycle.ARCHIVED):
			self.enabled = 0
		self.save(ignore_permissions=True)  # authz-ok: tier-b — gated by the caller's own permission check
		return self.lifecycle_state

	def assert_publishable(self):
		lifecycle.refuse(self.publish_problems(), _("This form cannot be published yet"))

	def freeze_version(self):
		"""Freeze the form as it stands as its current version: Publish means this form, not a later one."""
		return form_versions.ensure_version(self)

	def _definition_changed(self):
		before = self.get_doc_before_save()
		return not before or form_versions.build_payload(before) != form_versions.build_payload(self)

	def _refuse_editing_a_released_form(self, changed):
		"""A released form's definition changes only through a Draft, in Desk as in the builder."""
		before = self.get_doc_before_save()
		if before and not lifecycle.is_editable(before.lifecycle_state) and changed:
			frappe.throw(
				_("This form is {0}, not a Draft. Edit it to make a Draft; reps keep the published version until you publish again.").format(
					frappe.bold(_(before.lifecycle_state))),
				title=_("Not editable"),
			)

	# --- problems: the ONE reader. Shape is refused at every save; Publish adds what only the served version can tell ---

	def form_problems(self, mode=registry.PUBLISH):
		"""Every fault in this form as problem records. `DRAFT` is the shape a save refuses; `PUBLISH` adds a removed question
		something still names, a question moved off where its answers live, and a condition left naming a removed question."""
		served = self._served() if mode == registry.PUBLISH else None
		problems = [
			*self._label_problems(),
			*self._lead_source_problems(),
			*self._rule_problems(),
			*self._location_problems(),
			*self._link_problems(served if mode == registry.PUBLISH else self._stored()),
		]
		if served:
			problems += [*self._rebind_problems(served), *self._usage_problems(served), *self._dangling_problems(served)]
		return problems

	def publish_problems(self):
		return self.form_problems(registry.PUBLISH)

	def _stored(self):
		"""The form as stored, which a save is judged against: the before-image inside a save, else the stored row; None when new."""
		before = self.get_doc_before_save()
		if before or not (self.name and frappe.db.exists(self.doctype, self.name)):
			return before
		return frappe.get_doc(self.doctype, self.name)

	def _served(self):
		"""The version reps are offered now, or None for a form never published."""
		current = form_versions.current_name(self.name) if self.name else None
		return form_versions.load(current) if current else None

	def _label_problems(self):
		"""A row that asks the rep something must say what it asks. A LAYOUT row (`NO_VALUE_FIELDS` — Frappe's
		own list, the same one that keeps a Section Break out of a table's columns) stores nothing, so a Column
		Break carrying no heading is correct rather than incomplete."""
		return [
			_at(row.fieldname, _("Schema row {0}: a {1} field needs a label — it is what the rep is asked.").format(
				row.idx, row.fieldtype), "question.label", "label", _("Give it a label."))
			for row in self.schema
			if (row.fieldtype or "") not in NO_VALUE_FIELDS and not (row.label or "").strip()
		]

	def _lead_source_problems(self):
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
		problems = []
		for row in self.schema:
			fieldname = (row.fieldname or "").strip()
			if (row.get("source") or "") != LEAD_SOURCE or not fieldname:
				continue
			# Either door proves it is the lead's: a catalogued field (which may live on a child profile) or a plain column of CRM Lead itself.
			if fieldname not in catalogued and not lead_meta.get_field(fieldname):
				problems.append(_at(fieldname,
					_("Schema row {0}: `{1}` is sourced from the Lead but is neither a catalogued lead field "
					  "nor a column of CRM Lead. A lead-sourced row must carry the lead's own fieldname, or it "
					  "opens blank and snapshots under a name nothing answers to.").format(row.idx, fieldname),
					"question.not-a-lead-field", "fieldname", _("Bind it to a lead field, or make it a new answer.")))
		return problems

	def _rule_problems(self):
		"""Every rule row names fields THIS type declares, and a value the named field offers (§17.1).

		A rule is the form's behaviour, so a row naming a field that does not exist is a reaction that can
		never fire pointed at a target that can never be reached — silent on screen and impossible to find by
		reading a grid of nineteen rows. Each fault carries its ROW NUMBER, which is the only address an
		admin has for a child row, and `rule:<idx>`, which is the builder's.

		The declaration is the enforcement: the offered values are the field's own `options`, and the operator
		vocabulary is the compile's (`activity.api.RULE_VALUE_OPERATORS`) rather than restated here."""
		from tatva_connect.activity.api import RULE_SET_VALUE, rule_conditions, rule_targets

		rows, questions = self._declared_rows(), self._declared_questions()
		problems = []
		for row in self.rules:
			# Every triplet the compile reads is judged here — asked of `rule_conditions` so neither can see more of a row than the other.
			for field, operator, value in rule_conditions(row):
				problems += self._condition_problems(row, field, operator, value, rows, questions)
			if (row.action or "") == RULE_SET_VALUE:
				problems += self._copy_source_problems(row, questions)
			problems += [
				_at(_rule(row), _("Rule row {0}: {1} is not a field this task type declares.").format(row.idx, target),
					"rule.unknown-target", "targets", _("Pick a question or section this form has."))
				for target in rule_targets(row.targets) if target not in rows
			]
		return problems + self._copy_graph_problems()

	def _copy_graph_problems(self):
		"""Set Value copies must not run in a circle, across rows as well as within one.

		A cycle has no fixpoint, so nothing downstream can settle it: `copied_values` swaps the pair and
		stops on whichever parity its bound lands on, which makes a no-op re-save mutate the record — and
		the browser, which re-runs on its own reactivity, never converges at all. Refused because a
		graph is a property of the whole rule set and no single row can see it."""
		from tatva_connect.activity.api import RULE_SET_VALUE, rule_targets

		edges = {}
		for row in self.rules:
			if (row.action or "") != RULE_SET_VALUE:
				continue
			source = cstr(row.get("set_value") or "").strip()
			for target in rule_targets(row.targets):
				edges.setdefault(source, set()).add(target)
		seen, path, circles = set(), [], []

		def walk(node):
			if node in path:
				circles.append([*path[path.index(node):], node])
				return
			if node in seen:
				return
			seen.add(node)
			path.append(node)
			for nxt in edges.get(node, ()):
				walk(nxt)
			path.pop()

		for source in list(edges):
			walk(source)
		return [
			_at(circle[0], _("Set Value copies in a circle: {0}.").format(" → ".join(circle)), "rule.circular-copy",
				"set_value", _("Remove one of the copies so the chain ends."))
			for circle in circles
		]

	def _copy_source_problems(self, row, questions):
		"""A Set Value row copies one declared field into another, so its source must be a field that holds
		an answer — and never the target itself, which would copy a field onto itself and read as working.

		Checked here for the same reason a When value is: the declaration is the enforcement, and raw SQL
		aside, a source naming nothing is a rule that fires and copies blank over whatever the field held."""
		from tatva_connect.activity.api import LEAD_SOURCE, rule_targets

		source = cstr(row.get("set_value") or "").strip()
		if not source:
			return [_at(_rule(row), _("Rule row {0}: Set Value names {1} but declares no field to copy from.").format(
				row.idx, row.targets), "rule.no-source", "set_value", _("Pick the question to copy from."))]
		if source not in questions:
			return [_at(_rule(row), _("Rule row {0}: {1} is not a field this task type declares an answer for, so there is "
				"nothing to copy from it.").format(row.idx, source), "rule.unknown-source", "set_value",
				_("Pick a question this form asks."))]
		problems = []
		for target in rule_targets(row.targets):
			if target == source:
				problems.append(_at(_rule(row), _("Rule row {0}: Set Value copies {1} onto itself.").format(row.idx, source),
					"rule.copies-itself", "targets", _("Copy into a different question.")))
			# A layout row is a legitimate target for Show and Hide and holds nothing to write, so a copy
			# aimed at one reads as configured in the grid and silently does nothing.
			elif target not in questions:
				problems.append(_at(_rule(row), _("Rule row {0}: {1} holds no value, so Set Value has nowhere to copy into.").format(
					row.idx, target), "rule.not-a-question", "targets", _("Copy into a question, not a section.")))
			# The lead answers a lead-sourced field, so a copy would show one value read-only and store another.
			elif (questions[target].get("source") or "") == LEAD_SOURCE:
				problems.append(_at(_rule(row), _("Rule row {0}: {1} is answered by the lead, so Set Value cannot fill it.").format(
					row.idx, target), "rule.answered-by-lead", "targets", _("Copy into a question the rep answers.")))
		return problems

	def _condition_problems(self, row, field, operator, value, rows, questions):
		"""ONE When triplet: it names a field this type declares, that field holds an answer, and the value
		is one the field offers. Asked of both triplets so neither can be checked more loosely than the other."""
		from tatva_connect.activity.api import RULE_VALUE_OPERATORS

		field = (field or "").strip()
		if not field:
			return []
		if field not in rows:
			return [_at(_rule(row), _("Rule row {0}: {1} is not a field this task type declares.").format(row.idx, field),
				"rule.unknown-field", "condition_field", _("Pick a question this form asks."))]
		# A layout row holds no answer to read, so such a rule looks right in the grid and never fires; as a TARGET it is fine, that is how a section hides.
		if field not in questions:
			return [_at(_rule(row), _("Rule row {0}: {1} is a layout row and holds no value to test.").format(row.idx, field),
				"rule.not-a-question", "condition_field", _("Test a question, not a section."))]
		value = cstr(value or "").strip()
		if value and (operator or "").strip() in RULE_VALUE_OPERATORS:
			options = _options_of(questions[field])
			if options and value not in options:
				return [_at(_rule(row), _("Rule row {0}: {1} is not one of the options {2} declares.").format(row.idx, value, field),
					"rule.unknown-value", "condition_value", _("Pick one of that question's choices."))]
		return []

	def _declared_rows(self):
		"""Every declared row of this type, keyed by fieldname — layout markers INCLUDED.

		This is what a rule may TARGET: a Section Break is a legitimate target, because showing or hiding one
		is how a whole section appears. The two helpers here are the only definitions of "what this type
		declares" on this doctype, so no check builds its own."""
		return {(f.fieldname or "").strip(): f for f in self.schema if (f.fieldname or "").strip()}

	def _declared_questions(self):
		"""The declared rows that hold an ANSWER — layout markers excluded, because they store nothing.

		This is what any PREDICATE may name, whether it is a rule's When field or the location condition.
		Both ask this one helper, so the two can never disagree about what is askable."""
		return {name: f for name, f in self._declared_rows().items()
				if (f.fieldtype or "") not in NO_VALUE_FIELDS}

	def _location_problems(self):
		"""The location gate's condition must name a field THIS type declares, and a value that field offers.

		A predicate over answers the form does not collect is pointless: it can never hold, so the gate can
		never fire, and the type reads as location-guarded while guarding nothing. The condition is evaluated
		against this type's own settled answers (`location.api._condition_holds`), so the set a condition may
		name is exactly the set declared under Fields — the same relationship `_rule_problems` enforces for a
		rule's When field, checked here with the same two questions so the two cannot drift.

		Its own value list is skipped for `is set` / `is not set`, which ask only whether an answer exists —
		the same rule the compile applies (`RULE_VALUE_OPERATORS`)."""
		from tatva_connect.activity.api import RULE_VALUE_OPERATORS

		field = (self.get("location_condition_field") or "").strip()
		if not field:
			return []  # no condition declared: visit_mode alone decides, and that is a complete declaration
		questions = self._declared_questions()
		if field not in questions:
			return [_at(None,
				_("Location Required When names `{0}`, which is not a question this task type asks. A location "
				  "condition can only be asked of a field declared under Fields — otherwise it can never hold "
				  "and the location is never demanded.").format(field),
				"setting.location-field", "location_condition_field", _("Pick a question this form asks, or remove the location rule."))]
		value = cstr(self.get("location_condition_value") or "").strip()
		if value and (self.get("location_operator") or "").strip() in RULE_VALUE_OPERATORS:
			options = _options_of(questions[field])
			if options and value not in options:
				return [_at(None,
					_("Location Required When compares `{0}` against `{1}`, which is not one of the options "
					  "`{0}` declares.").format(field, value),
					"setting.location-value", "location_condition_value", _("Pick one of that question's choices."))]
		return []

	def _link_problems(self, baseline):
		"""A `Link` row must say WHICH doctype it links to, or the rep is handed a picker over nothing.

		`options` is free text because a `Select` row uses it for its newline-separated choices, so nothing
		ever checked the `Link` case. Two different failures, and they are judged differently:

		* **Naming a doctype that does not exist** is unambiguously an error, so it is refused outright.
		* **Naming nothing** is refused only on a row that is NEW or whose fieldtype/options just changed against
		  `baseline` (the stored form at a save, the served version at Publish). Several seeded types carry such
		  a row already (`Select ASM`, `Select CS Agent`, the `Select Junior Coach *` rows), and refusing them
		  outright would block an operator editing an unrelated field on a defect they did not introduce and
		  cannot safely fix, because what those fields actually hold is unknown. So the rule hardens every new
		  declaration and leaves the existing ones to be corrected deliberately.

		This is D-O's lesson applied again: scope a new rule to the TRANSITION and the old state keeps
		working. Rows are matched by key, the one identity a row keeps from a save to a frozen version.

		Deliberately a refusal and not a picker: the choice is one of a thousand doctypes, and a dropdown
		that long teaches nothing. A named refusal at authoring time does."""
		from tatva_connect.activity.api import LEAD_SOURCE

		before = {r.fieldname: r for r in (getattr(baseline, "schema", None) or [])}
		problems = []
		for row in self.schema:
			# A lead question takes its control from the lead column (`activity.api._stamp_lead_controls`), never its own options.
			if (row.fieldtype or "") != "Link" or (row.get("source") or "") == LEAD_SOURCE:
				continue
			target = (row.options or "").strip()
			if target and not frappe.db.exists("DocType", target):
				problems.append(_at(row.fieldname,
					_("Schema row {0}: `{1}` links to `{2}`, which is not a DocType on this site.").format(
						row.idx, row.label or row.fieldname, target),
					"question.unknown-doctype", "options", _("Name a record type that exists, for example User.")))
				continue
			if target:
				continue
			was = before.get(row.fieldname)
			if was and (was.fieldtype or "") == "Link" and not (was.options or "").strip():
				continue  # already in this state before: a pre-existing defect, not this edit's
			problems.append(_at(row.fieldname,
				_("Schema row {0}: `{1}` is a Link but names no DocType in Options, so the rep would be "
				  "offered a picker over nothing.").format(row.idx, row.label or row.fieldname),
				"question.link-target", "options", _("Name the record type it looks up, for example User.")))
		return problems

	def _rebind_problems(self, served):
		"""A published question keeps where its answers are stored: `field_target` reads `source`, `section` and `target`, and
		an old task's answers stay at the old address. Storing somewhere else is a new question."""
		was = {r.fieldname: r for r in served.schema if r.fieldname}
		return [
			_at(row.fieldname,
				_("{0} has stored its answers in one place since it was published; moving it would hide every answer already recorded.").format(
					frappe.bold(row.label or row.fieldname)),
				"question.rebound", "section", _("Keep its binding, or add a new question bound to the new place."))
			for row in self.schema
			if row.fieldname in was and (row.fieldtype or "") not in NO_VALUE_FIELDS and _home(row) != _home(was[row.fieldname])
		]

	def _usage_problems(self, served):
		"""A question the served version asks and this draft drops, while a workflow, Smart View or other consumer still names it."""
		hits = field_usage.task_type_hits(self, _keys(served) - _keys(self))
		return [
			_at(None, _("Cannot remove {0} because it is used by {1} {2} ({3})").format(field, _(dt), label or name, where),
				"usage.in-use", None, _("Repoint the {0} first, or keep the question.").format(_(dt)))
			for field, dt, name, label, where in hits
		]

	def _dangling_problems(self, served):
		"""A question's own condition (`depends_on`, `mandatory_depends_on`), or the picklist cascade narrowing its choices
		(`picklist.cascade_parent`), naming a question this draft drops: it can never hold again."""
		from tatva_connect.taxonomy import labels, picklist

		removed = _keys(served) - _keys(self)
		problems = []
		for row in self.schema:
			parent = (row.fieldtype or "") == "Link" and (row.options or "") == labels.PICKLIST_VALUE and picklist.cascade_parent(
				picklist.category_of(row.fieldname))
			if parent in removed:
				problems.append(_at(row.fieldname, _("{0} offers its choices by the answer to {1}, and this draft removes {1}.").format(
					frappe.bold(row.label or row.fieldname), parent), "question.condition-names-removed", "options",
					_("Keep the question its choices depend on.")))
			for column in ("depends_on", "mandatory_depends_on"):
				cond = (row.get(column) or "").strip()
				names = set(re.findall(r"\bdoc\.(\w+)", cond)) if cond.startswith("eval:") else {cond}
				problems += [
					_at(row.fieldname, _("{0} only shows or is required when {1} is answered, and this draft removes {1}.").format(
						frappe.bold(row.label or row.fieldname), name), "question.condition-names-removed", column,
						_("Clear the condition, or keep the question it reads."))
					for name in sorted(names & removed)
				]
		return problems


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
		task = frappe.get_meta("CRM Task")
		return [_column(c, labels.get(c) or c, task.get_field(c).fieldtype) for c in settable]
	target_doctype = frappe.get_cached_value("CRM Task Section", section, "target_doctype")
	if not target_doctype:
		return []  # a section that is not declared owns no columns; the router falls back and says so
	return [_column(f.fieldname, f.label or f.fieldname, f.fieldtype)
			for f in frappe.get_meta(target_doctype).fields
			if f.fieldtype not in NO_VALUE_FIELDS]


def _column(fieldname, label, fieldtype):
	"""One column home, with the question types that can be bound to it (`column_takes`)."""
	return {"fieldname": fieldname, "label": label, "takes": [t for t in question_types() if column_takes(fieldtype, t)]}


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


@frappe.whitelist()
def builder_doc(task_type):
	"""One Task Form as the SPA builder edits it, in ONE call.

	`doc` is exactly what `frappe.client.get` returns (same permission and field-level checks), `modified`
	included, so the builder's save can detect a conflict. `layout` is `activity.api.layout_tree` — the SAME
	walk the rep's form renders from — with every row kept and addressed by its child row `name`. `targets` is
	each rule's targets as `rule_targets` reads them. `settings` is stock crm's `get_fields_layout` for this doctype
	— the form's own settings as the SPA's `FieldLayout` renders them. Nothing here is a second reading of the
	declaration."""
	from crm.fcrm.doctype.crm_fields_layout.crm_fields_layout import get_fields_layout

	from tatva_connect.activity.api import layout_tree, rule_targets

	doc = frappe.get_doc("CRM Task Type", task_type)
	doc.check_permission()
	doc.apply_fieldlevel_read_permissions()

	def ref(row):
		return row.name if row else None

	layout = [
		{"row": ref(tab["row"]), "sections": [
			{"row": ref(section["row"]), "columns": [
				{"row": ref(column["row"]), "fields": [ref(d) for d in column["fields"]]}
				for column in section["columns"]]}
			for section in tab["sections"]]}
		for tab in layout_tree(doc.schema)]
	settings = get_fields_layout("CRM Task Type", "Data Fields")
	for tab in settings:
		for section in tab["sections"]:
			for column in section.get("columns") or []:
				# The two tables ARE the Design and Rules tabs, and Enabled is the header's lifecycle verb.
				column["fields"] = [f for f in column["fields"]
									if f.get("fieldtype") != "Table" and f.get("fieldname") != "enabled"]
	return {
		"doc": doc.as_dict(),
		# The version reps are offered now, as a workflow's header shows it; None before the first publish.
		"version": authoring_versions.current(form_versions.DOCTYPE, "task_type", doc.name, "question_count"),
		"layout": layout,
		"targets": {rule.name: rule_targets(rule.targets) for rule in doc.rules},
		"settings": settings,
		"can_write": bool(doc.has_permission("write")),
		# The legal lifecycle moves from here, as Desk's `__onload` carries them.
		"moves": lifecycle.moves(doc.lifecycle_state),
		# The Add Field picker: the child doctype's own question types, then the lead fields this grain offers.
		"question_types": question_types(),
		"lead_fields": list_lead_fields(doc.vertical, doc.group, doc.program),
		# Where a question can be bound, as Desk's Section and Target offer it: the lead snapshot section, and each column home.
		"bindings": _bindings(),
		# Fields the record's NAME is built from; renaming re-points every live CRM Task, so Settings locks them.
		"name_fields": re.findall(r"{(\w+)}", frappe.get_meta("CRM Task Type").autoname or ""),
		# The Rules tab's operator and action pickers, as the rule child doctype declares them.
		"rule_options": {f: (frappe.get_meta("CRM Task Type Rule").get_field(f).options or "").split("\n")
						 for f in ("operator", "action")},
	}


def _bindings():
	"""The homes a question may be bound to — the section a lead value is snapshotted into, and every column
	`list_target_columns` offers Desk's Target (the task's own settable columns, then each column section)."""
	sections = frappe.get_all(
		"CRM Task Section", fields=["name", "title", "is_key_value", "is_lead_snapshot"], order_by="display_order")
	homes = [{"section": "", "title": _("Task"), "columns": list_target_columns("")}]
	homes += [{"section": s.name, "title": _(s.title), "columns": list_target_columns(s.name)}
			  for s in sections if not s.is_key_value]
	return {
		"lead_section": next((s.name for s in sections if s.is_lead_snapshot), None),
		"activity": [h for h in homes if h["columns"]],
	}


TASK_TYPE = "CRM Task Type"


@frappe.whitelist(methods=["POST"])
def save_draft(doc):
	"""Persist the builder's Draft through the form's own save (permission, timestamp, `validate`), as a workflow's Draft save.

	Shape faults come back as DATA before anything is written, every one at once; a saved Draft answers with the form as
	stored and what would block a Publish, so the builder marks it now rather than at Publish."""
	draft = frappe.get_doc(frappe.parse_json(doc))
	draft.check_permission("write")
	shape = registry.blocking(draft.form_problems(registry.DRAFT))
	if shape:
		return {"saved": False, "problems": shape, "summary": registry.problem_summary(shape)}
	draft.save()
	return {"saved": True, **builder_doc(draft.name), "problems": registry.blocking(draft.publish_problems())}


@frappe.whitelist(methods=["POST"])
def publish(name):
	"""Draft -> Published, as a workflow publishes. A form reps were already offered goes straight on to Active (a legal
	move), so its new version reaches them on publish under the same name and nothing that names the form changes."""
	offered = frappe.db.get_value(TASK_TYPE, name, "enabled")
	answer = lifecycle.publish(TASK_TYPE, name)
	if answer["ok"] and offered:
		answer.update(lifecycle.transition(TASK_TYPE, name, lifecycle.ACTIVE))
	return answer


@frappe.whitelist(methods=["POST"])
def activate(name):
	"""Published/Suspended -> Active: reps are offered the current version from their next open."""
	return lifecycle.transition(TASK_TYPE, name, lifecycle.ACTIVE)


@frappe.whitelist(methods=["POST"])
def suspend(name):
	"""Active -> Suspended: reps stop being offered the form; every task it recorded stays readable on its version."""
	return lifecycle.transition(TASK_TYPE, name, lifecycle.SUSPENDED)


@frappe.whitelist(methods=["POST"])
def revise(name):
	"""Published/Active/Suspended -> Draft: reopen for editing. Reps keep the published version until the Draft is published."""
	return lifecycle.transition(TASK_TYPE, name, lifecycle.DRAFT)


@frappe.whitelist(methods=["POST"])
def archive(name):
	"""-> Archived (terminal): retire the form. Its versions stay, so every task it recorded stays readable."""
	return lifecycle.transition(TASK_TYPE, name, lifecycle.ARCHIVED)


@frappe.whitelist()
def submission_counts(task_type):
	"""How this form has been used — the Submissions cards, in Workflows' `run_counts` shape.

	Counted by `get_list` over `CRM Task`, so the viewer's own permissions and row scope decide what is counted: a
	manager sees their line's numbers. One card per `due_state` bucket, read through `list_engine.derived` — the
	same brain the Tasks list filters by — and each carries the dashboard's drill into that list, never tuples."""
	from tatva_connect.dashboard.executor import _ROUTES
	from tatva_connect.list_engine import derived

	frappe.has_permission("CRM Task Type", "read", task_type, throw=True)
	base = [["CRM Task", "custom_task_type", "=", task_type]]

	def drill(extra):
		return {"route": _ROUTES["CRM Task"], "filters": {"custom_task_type": task_type, **extra}}

	total = frappe.get_list("CRM Task", filters=base, fields=[{"COUNT": "*", "as": "n"}, {"MAX": "creation", "as": "last"}])[0]
	field = derived.get("CRM Task", "due_state")
	snap = derived.snapshot()
	return {
		"total": total.n,
		"last_logged_at": total.last,
		"drill": drill({}),
		"buckets": [
			{
				"bucket": bucket.value,
				"total": frappe.get_list(
					"CRM Task", filters=[*base, *derived.resolve(field, bucket, snap)], fields=[{"COUNT": "*", "as": "n"}]
				)[0].n,
				"drill": drill({field.fieldname: bucket.value}),
			}
			for bucket in (field.buckets if field else [])
		],
	}
