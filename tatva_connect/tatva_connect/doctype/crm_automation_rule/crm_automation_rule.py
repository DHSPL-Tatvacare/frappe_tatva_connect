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
		self._validate_child_actions()
		self._validate_webhook_actions()

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
		"""Each criterion's field must exist on the trigger task type's activity form, and its operator
		must be valid for that field's type. Both resolved from the one describe contract — the same
		source the builder renders from, so the form and the validator never drift."""
		from tatva_connect.automation.describe import fields_for_task_type

		descriptors = {f["key"]: f for f in fields_for_task_type(self.task_type)}
		if not descriptors:
			return  # no schema to check against — leave it (best-effort)
		for c in self.criteria:
			if not c.field:
				continue
			d = descriptors.get(c.field)
			if d is None:
				frappe.throw(
					_("Criterion field {0} is not a field on task type {1}.").format(
						frappe.bold(c.field), frappe.bold(self.task_type)
					),
					title=_("Unknown criterion field"),
				)
			elif c.operator and c.operator not in d["operators"]:
				frappe.throw(
					_("Operator {0} can't be used on {1} (a {2} field).").format(
						frappe.bold(c.operator), frappe.bold(c.field), d["type"]
					),
					title=_("Operator not valid for field"),
				)

	def _validate_set_field_actions(self):
		"""Every Set Field target (doctype + fieldname) must be in the enabled Automatable-Field
		allowlist whose grain matches this rule's grain. Fail-closed (spec §5.3)."""
		for a in self.actions:
			if a.action_type != "Set Field":
				continue
			if not (a.target_doctype and a.fieldname):
				frappe.throw(_("A Set Field action needs both a Target DocType and a Fieldname."), title=_("Incomplete action"))
			if a.value_mode not in ("Literal", "From Context"):
				frappe.throw(_("A Set Field action needs a Value Mode (Literal or From Context)."), title=_("Incomplete action"))
			if a.value_mode == "From Context" and not a.context_field:
				frappe.throw(_("'From Context' needs a Context Field."), title=_("Incomplete action"))
			if not self._allowlisted(a.target_doctype, a.fieldname):
				frappe.throw(
					_("Field {0} on {1} is not in the Automatable-Field allowlist for this grain. Add it (enabled) before a rule can set it.").format(
						frappe.bold(a.fieldname), frappe.bold(a.target_doctype)
					),
					title=_("Field not allowlisted"),
				)

	def _validate_child_actions(self):
		"""Append/Upsert Child Row: the child table must be a Table field on CRM Lead; every set (and
		match) field must be allowlisted for the child doctype at this grain; upsert match keys must be
		marked is_row_key. Fail-closed (spec §4.2/§5.3)."""
		for a in self.actions:
			if a.action_type not in ("Append Child Row", "Upsert Child Row"):
				continue
			if not a.child_table:
				frappe.throw(_("A child-row action needs a Child Table."), title=_("Incomplete action"))
			field = frappe.get_meta("CRM Lead").get_field(a.child_table)
			if not field or field.fieldtype != "Table":
				frappe.throw(_("{0} is not a child table on CRM Lead.").format(frappe.bold(a.child_table)), title=_("Bad child table"))
			child_dt = field.options
			set_map = _parse_json(a.set_json, _("Set (JSON)"))
			match_map = _parse_json(a.match_json, _("Match (JSON)")) if a.action_type == "Upsert Child Row" else {}
			if a.action_type == "Upsert Child Row" and not match_map:
				frappe.throw(_("Upsert Child Row needs a Match (JSON)."), title=_("Incomplete action"))
			if a.action_type == "Append Child Row" and not set_map:
				frappe.throw(_("Append Child Row needs a Set (JSON)."), title=_("Incomplete action"))
			overlap = set(set_map) & set(match_map)
			if overlap:
				frappe.throw(
					_("A child-row action's Set fields must not include its Match keys: {0}").format(", ".join(sorted(overlap))),
					title=_("Set / Match overlap"),
				)
			for f in set(set_map) | set(match_map):
				if not self._allowlisted(child_dt, f, child_table=a.child_table, require_row_key=(f in match_map)):
					frappe.throw(
						_("Field {0} on {1} ({2}) is not in the Automatable-Field allowlist for this grain.").format(
							frappe.bold(f), frappe.bold(child_dt), frappe.bold(a.child_table)
						),
						title=_("Field not allowlisted"),
					)

	def _validate_webhook_actions(self):
		"""Call Webhook: the endpoint must be set and exist (spec §6). URL/secret stay admin-curated."""
		for a in self.actions:
			if a.action_type != "Call Webhook":
				continue
			if not a.webhook_endpoint:
				frappe.throw(_("A Call Webhook action needs an endpoint."), title=_("Incomplete action"))
			if not frappe.db.exists("Webhook", a.webhook_endpoint):
				frappe.throw(_("Webhook endpoint {0} does not exist.").format(frappe.bold(a.webhook_endpoint)), title=_("Unknown endpoint"))

	def _allowlisted(self, target_doctype, fieldname, child_table=None, require_row_key=False):
		"""True if an enabled allowlist row covers (doctype, field) at a grain compatible with this
		rule: each SET allowlist axis must equal the rule's value; a BLANK allowlist axis is a wildcard.
		For child-row targets the row must also match the child table (and be a row-key when required)."""
		filters = {"target_doctype": target_doctype, "fieldname": fieldname, "enabled": 1}
		if child_table is not None:
			filters["child_table_field"] = child_table
		if require_row_key:
			filters["is_row_key"] = 1
		from tatva_connect.automation import rules

		rows = frappe.get_all("CRM Automatable Field", filters=filters, fields=["vertical", "group", "program"])
		return any(rules.grain_matches(r, self.vertical, self.group, self.program) for r in rows)


def _parse_json(raw, label):
	"""Parse a child-row JSON map; throw a clear authoring error if it's not a JSON object."""
	if not (raw or "").strip():
		return {}
	try:
		data = frappe.parse_json(raw)
	except Exception:
		data = None
	if isinstance(data, dict):
		return data
	frappe.throw(_("{0} must be a JSON object like {{\"field\": \"value\"}}.").format(label), title=_("Bad JSON"))
	return {}  # unreachable (frappe.throw raises) — keeps the return type a clean dict
