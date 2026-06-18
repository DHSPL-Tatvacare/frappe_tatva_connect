# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class CRMAutomationRule(Document):
	def validate(self):
		# Authoring guardrails (spec §5.4): hard errors block save. Fail-closed — a rule that
		# can't be proven safe never saves. No engine imports here (avoid cycles).
		self._require_grain()
		self._require_task_type()
		self._validate_criteria_fields()
		self._validate_set_field_actions()

	def _require_grain(self):
		"""No global rule: at least one grain axis must be set (invariant #11 / spec §5.1)."""
		if not (self.vertical or self.group or self.program):
			frappe.throw(
				_("A rule must declare at least one grain axis (Vertical / Group / Program). A grain-less rule is not allowed."),
				title=_("No grain"),
			)

	def _require_task_type(self):
		"""The trigger task type and every Create Task target must exist as a CRM Task Type."""
		referenced = {self.task_type}
		for a in self.actions:
			if a.action_type == "Create Task" and a.task_type:
				referenced.add(a.task_type)
		for tt in referenced:
			if tt and not frappe.db.exists("CRM Task Type", tt):
				frappe.throw(_("Task Type {0} does not exist.").format(frappe.bold(tt)), title=_("Unknown task type"))

	def _validate_criteria_fields(self):
		"""Best-effort: a criterion's field should be a schema fieldname on the trigger task type's
		activity form. Throw only when clearly invalid (the type has a schema and the field isn't in it)."""
		schema = {
			r.fieldname
			for r in frappe.get_all(
				"CRM Task Type Field",
				filters={"parent": self.task_type, "parenttype": "CRM Task Type"},
				fields=["fieldname"],
			)
		}
		if not schema:
			return  # no schema to check against — leave it (best-effort)
		for c in self.criteria:
			if c.field and c.field not in schema:
				frappe.throw(
					_("Criterion field {0} is not a field on task type {1}.").format(
						frappe.bold(c.field), frappe.bold(self.task_type)
					),
					title=_("Unknown criterion field"),
				)

	def _validate_set_field_actions(self):
		"""Every Set Field target (doctype + fieldname) must be in the enabled Automatable-Field
		allowlist whose grain matches this rule's grain. Fail-closed (spec §5.3)."""
		for a in self.actions:
			if a.action_type != "Set Field":
				continue
			if not (a.target_doctype and a.fieldname):
				frappe.throw(_("A Set Field action needs both a Target DocType and a Fieldname."), title=_("Incomplete action"))
			if not self._allowlisted(a.target_doctype, a.fieldname):
				frappe.throw(
					_("Field {0} on {1} is not in the Automatable-Field allowlist for this grain. Add it (enabled) before a rule can set it.").format(
						frappe.bold(a.fieldname), frappe.bold(a.target_doctype)
					),
					title=_("Field not allowlisted"),
				)

	def _allowlisted(self, target_doctype, fieldname):
		"""True if an enabled allowlist row covers (doctype, field) at a grain compatible with this
		rule: each SET allowlist axis must equal the rule's value; a BLANK allowlist axis is a wildcard."""
		rows = frappe.get_all(
			"CRM Automatable Field",
			filters={"target_doctype": target_doctype, "fieldname": fieldname, "enabled": 1},
			fields=["vertical", "group", "program"],
		)
		for r in rows:
			if self._grain_matches(r):
				return True
		return False

	def _grain_matches(self, row):
		for axis in ("vertical", "group", "program"):
			rval = row.get(axis) or ""
			if rval and rval != (self.get(axis) or ""):
				return False
		return True
