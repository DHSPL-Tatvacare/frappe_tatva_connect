# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model import NO_VALUE_FIELDS
from frappe.model.document import Document
from frappe.utils import cstr

from tatva_connect.taxonomy.normalize import normalize_field


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
		from tatva_connect.activity.api import RULE_VALUE_OPERATORS

		declared = {(f.fieldname or "").strip(): f for f in self.schema if (f.fieldname or "").strip()}
		for row in self.rules:
			field = (row.condition_field or "").strip()
			if field and field not in declared:
				frappe.throw(_("Rule row {0}: {1} is not a field this task type declares.").format(row.idx, field),
							 title=_("Unknown field"))
			# A layout row holds no answer to read, so such a rule looks right in the grid and never fires; as a TARGET it is fine, that is how a section hides.
			if field and (declared[field].fieldtype or "") in NO_VALUE_FIELDS:
				frappe.throw(_("Rule row {0}: {1} is a layout row and holds no value to test.").format(row.idx, field),
							 title=_("Not a question"))
			value = cstr(row.condition_value or "").strip()
			if field and value and (row.operator or "").strip() in RULE_VALUE_OPERATORS:
				options = [o.strip() for o in (declared[field].options or "").split("\n") if o.strip()]
				if options and value not in options:
					frappe.throw(
						_("Rule row {0}: {1} is not one of the options {2} declares.").format(row.idx, value, field),
						title=_("Unknown value"))
			for target in [t.strip() for t in (row.targets or "").split(",") if t.strip()]:
				if target not in declared:
					frappe.throw(
						_("Rule row {0}: {1} is not a field this task type declares.").format(row.idx, target),
						title=_("Unknown target"))

	def _declared_questions(self):
		"""The fields of this type that hold an ANSWER a condition can be asked about — every declared row
		except the layout markers, which store nothing. Asked by both condition validators below so
		"what may a predicate name" has one definition on this doctype."""
		return {(f.fieldname or "").strip(): f for f in self.schema
				if (f.fieldname or "").strip() and (f.fieldtype or "") not in NO_VALUE_FIELDS}

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
		declared = self._declared_questions()
		if field not in declared:
			frappe.throw(
				_("Location Required When names `{0}`, which is not a question this task type asks. A location "
				  "condition can only be asked of a field declared under Fields — otherwise it can never hold "
				  "and the location is never demanded.").format(field),
				title=_("Not a declared field"))
		value = cstr(self.get("location_condition_value") or "").strip()
		if value and (self.get("location_operator") or "").strip() in RULE_VALUE_OPERATORS:
			options = [o.strip() for o in (declared[field].options or "").split("\n") if o.strip()]
			if options and value not in options:
				frappe.throw(
					_("Location Required When compares `{0}` against `{1}`, which is not one of the options "
					  "`{0}` declares.").format(field, value),
					title=_("Unknown value"))

	def _validate_link_fields_name_a_doctype(self):
		"""A `Link` row must say WHICH doctype it links to, or the rep is handed a picker over nothing.

		`options` is free text because a `Select` row uses it for its newline-separated choices, so nothing
		ever checked the `Link` case — and the seeds carry rows like `Select Junior Coach - Diet` and
		`Select ASM` that declare `Link` with no target at all. Those fields cannot be answered.

		Deliberately a REFUSAL and not a picker: the choice is one of a thousand doctypes, and a dropdown that
		long teaches nothing. A named refusal at authoring time does."""
		for row in self.schema:
			if (row.fieldtype or "") != "Link":
				continue
			target = (row.options or "").strip()
			if not target:
				frappe.throw(
					_("Schema row {0}: `{1}` is a Link but names no DocType in Options, so the rep would be "
					  "offered a picker over nothing.").format(row.idx, row.label or row.fieldname),
					title=_("Link needs a target"))
			if not frappe.db.exists("DocType", target):
				frappe.throw(
					_("Schema row {0}: `{1}` links to `{2}`, which is not a DocType on this site.").format(
						row.idx, row.label or row.fieldname, target),
					title=_("Unknown DocType"))


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
		return [{"fieldname": c, "label": c} for c in task_columns()]
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
