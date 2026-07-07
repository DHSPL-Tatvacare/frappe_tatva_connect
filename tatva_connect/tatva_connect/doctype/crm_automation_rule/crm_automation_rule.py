# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

# TATVA v2 (Task 1): the frozen operator vocabulary (plan Part A) - a flat membership check only.
# Task 3 replaces this with a per-field-type table once describe.OPERATORS_BY_TYPE (still the old
# symbol set: "=", "!=", "like", ...) is reshaped onto these word operators; the builder (Task 14)
# then re-derives the same table from describe.builder_schema so this constant retires there.
_OPERATORS = frozenset({
	"is", "is not", "greater than", "less than", "at least", "at most",
	"is one of", "is not one of", "contains", "does not contain",
	"is set", "is not set", "is between", "changed to", "changed from…to",
})
_CHANGED_OPERATORS = frozenset({"changed to", "changed from…to"})


class CRMAutomationRule(Document):
	def validate(self):
		# Authoring guardrails (spec §5.4): hard errors block save. Fail-closed - a rule that
		# can't be proven safe never saves. No engine imports here (avoid cycles).
		self._require_grain()
		self._require_trigger()
		self._require_action_task_types_exist()
		self._validate_criteria_fields()
		self._validate_set_field_actions()
		self._validate_create_task_actions()
		self._validate_create_note_actions()
		self._validate_child_actions()
		self._validate_webhook_actions()

	def _require_grain(self):
		"""No global rule: at least one grain axis must be set (invariant #11 / spec §5.1)."""
		if not (self.vertical or self.group or self.program):
			frappe.throw(
				_("A rule must declare at least one grain axis (Vertical / Group / Program). A grain-less rule is not allowed."),
				title=_("No grain"),
			)

	def _require_trigger(self):
		"""TATVA v2 (Task 1): every rule now triggers on (on_doctype, event) - the old Task-Completed
		vs Field-Changed split collapses into one shape (a "task completed" rule becomes
		on_doctype="CRM Task", event="Updated" + a criterion `status changed to Done` - see
		patches.reshape_automation_triggers). on_doctype must be a grain-resolvable subject (v1: CRM
		Lead / CRM Task) - a rule can't claim grain-scoping the dispatcher can't honor. Full per-doctype
		activation (any doctype, no per-doctype code push) is Task 4's wildcard router."""
		if not (self.on_doctype and self.event):
			frappe.throw(
				_("A rule needs both an On DocType and an Event."),
				title=_("Incomplete trigger"),
			)
		if self.event not in ("Created", "Updated", "Deleted"):
			# Belt-and-braces alongside the native Select-option check (defense in depth, not a
			# parallel brain — same frozen set as the doctype JSON's event options).
			frappe.throw(
				_("{0} is not a valid Event (Created, Updated, Deleted).").format(frappe.bold(self.event)),
				title=_("Invalid event"),
			)
		from tatva_connect.automation import subjects

		if not subjects.is_subject(self.on_doctype):
			frappe.throw(
				_("{0} is not a supported trigger doctype. A new subject needs a resolver in automation.subjects first.").format(
					frappe.bold(self.on_doctype)
				),
				title=_("Unsupported trigger doctype"),
			)

	def _require_action_task_types_exist(self):
		"""Every Create Task action's target task type must exist (trigger-agnostic - a Create Task
		action is valid on either trigger)."""
		for a in self.actions:
			if a.action_type == "Create Task" and a.task_type:
				if not frappe.db.exists("CRM Task Type", a.task_type):
					frappe.throw(_("Task Type {0} does not exist.").format(frappe.bold(a.task_type)), title=_("Unknown task type"))

	def _criteria_vocabulary(self):
		"""The {fieldname: descriptor} map a rule's criteria are validated against - the on_doctype's
		own meta (fields_for_doctype). One describe contract - the builder and the validator read the
		same vocabulary, so they can't drift (spec §5.4). TATVA v2 (Task 1): the old Task-Completed
		branch (fields_for_task_type) is retired with trigger_type/task_type; Task 2 replaces this
		whole resolver with the typed field_catalog (Link/Select/child-table pick sources)."""
		from tatva_connect.automation.describe import fields_for_doctype

		return {f["key"]: f for f in fields_for_doctype(self.on_doctype)}

	def _validate_criteria_fields(self):
		"""Each criterion's field must exist in the trigger's vocabulary, and its operator must be a
		recognised v2 operator (Part A). `changed to` / `changed from…to` are transition operators,
		not field-type operators - see `_validate_changed_operator`. TATVA v2 (Task 1): the operator
		check is a flat frozen-set membership check (`_OPERATORS`), not yet per-field-type - Task 3
		reshapes describe.OPERATORS_BY_TYPE onto the word set and this re-derives from that instead."""
		descriptors = self._criteria_vocabulary()
		for c in self.criteria:
			if not c.field:
				continue
			if c.operator and c.operator not in _OPERATORS:
				frappe.throw(
					_("Operator {0} is not a recognised automation operator.").format(frappe.bold(c.operator)),
					title=_("Unknown operator"),
				)
			if c.operator in _CHANGED_OPERATORS:
				self._validate_changed_operator(c)
				continue
			d = descriptors.get(c.field) if descriptors else None
			if d is None:
				frappe.throw(
					_("Criterion field {0} is not a field on {1}.").format(
						frappe.bold(c.field), frappe.bold(self.on_doctype)
					),
					title=_("Unknown criterion field"),
				)

	def _validate_changed_operator(self, c):
		"""`changed to` (new value only) and `changed from…to` (old->new pair) are the narrowed-
		transition operators (Part A): valid only when event=Updated (Created has no before-state;
		Deleted fires pre-removal with no diff). TATVA v2 (Task 1): the old restriction to a single
		rule-wide watch_field is dropped - v2 has no single watch_field, and the router (Task 4)
		carries `{field}__before` for every criteria-referenced field, not just one."""
		if self.event != "Updated":
			frappe.throw(
				_("Operator {0} is only valid when the rule's Event is Updated.").format(frappe.bold(c.operator)),
				title=_("Operator not valid for event"),
			)
		if c.operator == "changed from…to" and not (c.from_value and c.value):
			frappe.throw(
				_("Operator {0} needs both a From Value and a Value.").format(frappe.bold("changed from…to")),
				title=_("Incomplete criterion"),
			)
		if c.operator == "changed to" and not c.value:
			frappe.throw(
				_("Operator {0} needs a Value.").format(frappe.bold("changed to")),
				title=_("Incomplete criterion"),
			)

	def _validate_set_field_actions(self):
		"""Every Update Field target (doctype + fieldname) must be in the enabled Automatable-Field
		allowlist whose grain matches this rule's grain. Fail-closed (spec §5.3). The value_mode
		Expression requires a parseable expression (syntax-only - a live dry-run would falsely block
		a legitimate ctx[...] ref on an empty author-time context). TATVA v2 (Task 1): action_type
		renamed Set Field -> Update Field to match the reshaped crm_automation_action.json verb set."""
		from tatva_connect.automation import expr, fields

		for a in self.actions:
			if a.action_type != "Update Field":
				continue
			if not (a.target_doctype and a.fieldname):
				frappe.throw(_("An Update Field action needs both a Target DocType and a Fieldname."), title=_("Incomplete action"))
			if a.target_doctype not in self._set_field_scope():
				frappe.throw(
					_("Update Field target {0} is out of scope — a rule may only set a field on the Lead or the triggering {1}.").format(
						frappe.bold(a.target_doctype), frappe.bold(self._trigger_doctype() or "record")
					),
					title=_("Target out of scope"),
				)
			if a.value_mode not in ("Literal", "From Context", "Expression"):
				frappe.throw(_("An Update Field action needs a Value Mode (Literal, From Context, or Expression)."), title=_("Incomplete action"))
			if a.value_mode == "From Context" and not a.context_field:
				frappe.throw(_("'From Context' needs a Context Field."), title=_("Incomplete action"))
			if a.value_mode == "Expression":
				if not (a.expression or "").strip():
					frappe.throw(_("An Expression Update Field needs an Expression."), title=_("Incomplete action"))
				expr.assert_parses(a.expression)
			if not fields.is_settable(a.target_doctype, a.fieldname, (self.vertical, self.group, self.program)):
				frappe.throw(
					_("Field {0} on {1} is not in the Automation-Field set allowlist for this grain. Add it (Can Set, enabled) before a rule can set it.").format(
						frappe.bold(a.fieldname), frappe.bold(a.target_doctype)
					),
					title=_("Field not allowlisted"),
				)

	def _trigger_doctype(self):
		"""The doctype of the record that fires this rule. TATVA v2 (Task 1): just on_doctype now -
		the old Task-Completed special case (always "CRM Task") collapses into the same field."""
		return self.on_doctype

	def _set_field_scope(self):
		"""The doctypes a Set Field may target — the runtime write scope: the Lead, plus the trigger
		doc's own type (a Field-Changed rule may set a field on the watched doc; a Task-Completed rule
		on the completed task). Mirrors dispatcher._resolve_write_target so a rule can't save a target
		the dispatcher would only ever reject."""
		scope = {"CRM Lead"}
		trigger = self._trigger_doctype()
		if trigger:
			scope.add(trigger)
		return scope

	def _validate_create_task_actions(self):
		"""Create Task: due_mode Expression needs a parseable due_expression (syntax-only)."""
		from tatva_connect.automation import expr

		for a in self.actions:
			if a.action_type != "Create Task":
				continue
			if not a.task_type:
				frappe.throw(_("A Create Task action needs a Task Type."), title=_("Incomplete action"))
			if a.due_mode == "Expression":
				if not (a.due_expression or "").strip():
					frappe.throw(_("An Expression Create Task needs a Due Expression."), title=_("Incomplete action"))
				expr.assert_parses(a.due_expression)

	def _validate_create_note_actions(self):
		"""Create Note: comment_mode Literal needs text; Expression needs a parseable expression.
		TATVA v2 (Task 1): action_type renamed Add Comment -> Create Note to match the reshaped
		crm_automation_action.json verb set (field names comment_mode/comment_text/comment_expression
		are unchanged - reused as-is per Part B)."""
		from tatva_connect.automation import expr

		for a in self.actions:
			if a.action_type != "Create Note":
				continue
			if a.comment_mode == "Literal":
				if not (a.comment_text or "").strip():
					frappe.throw(_("A Literal Create Note needs Comment Text."), title=_("Incomplete action"))
			elif a.comment_mode == "Expression":
				if not (a.comment_expression or "").strip():
					frappe.throw(_("An Expression Create Note needs a Comment Expression."), title=_("Incomplete action"))
				expr.assert_parses(a.comment_expression)
			else:
				frappe.throw(_("A Create Note needs a Comment Mode (Literal or Expression)."), title=_("Incomplete action"))

	def _validate_child_actions(self):
		"""Append/Upsert Child Row: the child table must be a Table field on CRM Lead; every set (and
		match) field must be allowlisted for the child doctype at this grain; upsert match keys must be
		marked is_row_key. Fail-closed (spec §4.2/§5.3)."""
		from tatva_connect.automation import fields

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
				if not fields.is_settable(
					child_dt, f, (self.vertical, self.group, self.program),
					child_table_field=a.child_table, require_row_key=(f in match_map),
				):
					frappe.throw(
						_("Field {0} on {1} ({2}) is not in the Automation-Field set allowlist for this grain.").format(
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

	def on_change(self):
		"""TATVA v2 (Task 4): the wildcard router's `live_doctypes()` guard set is derived from every
		ENABLED rule's on_doctype and cached - the ONE clear point, so an enable/disable/on_doctype
		edit takes effect at once (no per-doctype hook to keep in sync, A.8)."""
		from tatva_connect.automation import router

		router.clear_live_doctypes_cache()

	def on_trash(self):
		"""Same cache-clear as on_change - a deleted rule must drop out of live_doctypes() too."""
		from tatva_connect.automation import router

		router.clear_live_doctypes_cache()

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
