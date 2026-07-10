# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

_CHANGED_OPERATORS = frozenset({"changed to", "changed from…to"})


class CRMAutomationRule(Document):
	def validate(self):
		# Authoring guardrails (spec §5.4): hard errors block save. Fail-closed - a rule that
		# can't be proven safe never saves. No engine imports here (avoid cycles).
		self._require_grain()
		self._require_trigger()
		self._validate_action_verbs_registered()
		self._require_action_task_types_exist()
		self._validate_criteria_fields()
		self._validate_set_field_actions()
		self._validate_create_task_actions()
		self._validate_create_note_actions()
		self._validate_child_actions()
		self._validate_webhook_actions()
		self._validate_send_whatsapp()
		self._validate_wait_actions()
		self._plan_in_flight_migration()

	def on_update(self):
		"""Freeze this definition and move every in-flight lead the edit provably did not disturb onto
		it — inside the rule's OWN save transaction, so the program and its executions advance together
		or not at all. The plan was dry-run in validate(), so nothing here can raise a surprise."""
		from tatva_connect.automation import versions

		version = versions.ensure_version(self)
		migrated, retained = versions.apply_migration(self._migration_plan, version)
		if not (migrated or retained):
			return
		summary = _(
			"Version {0}: {1} in-flight lead(s) adopted it; {2} stay on their prior version because this "
			"edit changed a step they had already passed."
		).format(frappe.bold(version), migrated, retained)
		self.add_comment("Info", summary)  # the audit trail lives on the rule, beside its Version history
		frappe.msgprint(summary, title=_("In-flight leads"), indicator="blue")

	def _plan_in_flight_migration(self):
		"""DRY RUN, before anything commits: decide per parked execution whether it may adopt this new
		definition, and precompute its rescheduled wake time. Raises — and so blocks the save in the Desk
		form — when a lead cannot be rescheduled under the edited Wait. `on_update` applies this exact
		plan, so the decision and the write can never drift."""
		from tatva_connect.automation import versions

		self._migration_plan = versions.plan_migration(self)

	def _validate_wait_actions(self):
		"""A Wait is a segment boundary, so it must have something to resume INTO, and its delay must BE
		a delay. Both are verb-agnostic misconfiguration, caught at author time instead of months later
		on a sweep (a Wait with nothing after it used to log a silent 'Success, 0 actions').

		A context-FREE expression is a constant, so evaluate it for real and reject a bad value. One that
		reads `ctx` can only be checked for syntax: the author-time context is empty, and `ctx.get("x")`
		would legitimately resolve to `None` there — the same reasoning `expr.assert_parses` documents."""
		from tatva_connect.automation import actions, expr

		effect_actions = [a for a in self.actions if actions._ACTION_LANES.get(a.action_type, (None, None))[0] == "effect"]
		for position, action in enumerate(effect_actions):
			if action.action_type != "Wait":
				continue
			if position == len(effect_actions) - 1:
				frappe.throw(
					_("A Wait must be followed by at least one action to resume into — row {0} is the last one.").format(action.idx),
					title=_("Wait resumes into nothing"),
				)
			if expr.references_context(action.wait_expression):
				expr.assert_parses(action.wait_expression)
			else:
				actions.wait_resume_at(action.wait_expression, {}, frappe.utils.now_datetime())

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

	def _validate_action_verbs_registered(self):
		"""Every action's verb must have a registered handler (Task 8's soul-check gap closer): a verb
		sitting in the action_type Select with no `_ACTION_LANES` entry (e.g. Wait, before Task 9) must
		fail LOUD at author time, not silently at fire time. Imports the registry off `actions` — the
		single source of verb->lane truth (A.8), never a second verb list here."""
		from tatva_connect.automation import actions

		for a in self.actions:
			if a.action_type and a.action_type not in actions._ACTION_LANES:
				frappe.throw(
					_("{0} has no registered handler yet and cannot be used in a rule.").format(frappe.bold(a.action_type)),
					title=_("Unknown action verb"),
				)

	def _require_action_task_types_exist(self):
		"""Every Create Task action's target task type must exist (trigger-agnostic - a Create Task
		action is valid on either trigger)."""
		for a in self.actions:
			if a.action_type == "Create Task" and a.task_type:
				if not frappe.db.exists("CRM Task Type", a.task_type):
					frappe.throw(_("Task Type {0} does not exist.").format(frappe.bold(a.task_type)), title=_("Unknown task type"))

	def _builder_schema(self):
		"""Re-derive the SAME contract the Rule Form Script rendered from (describe.builder_schema) -
		the ONE emitter, called here to re-enforce it at save time (Task 14 / plan Part G). A key the
		schema doesn't offer can therefore never be saved, no matter what the frontend sends."""
		from tatva_connect.automation import describe

		return describe.builder_schema(self.on_doctype, self.event, self.vertical, self.group, self.program)

	def _validate_criteria_fields(self):
		"""Each criterion's field/operator/value must all be inside the SAME contract the builder
		offered (describe.builder_schema): the field must be in the can_watch-scoped `fields` catalog,
		the operator must be valid for that field's schema type (`operators_by_type`), and the value
		must coerce to that type (describe.coerces). `changed to` / `changed from…to` are additionally
		gated to event=Updated - see `_validate_changed_operator`. Fail-closed, one contract (A.8) -
		this REPLACES the old flat `_OPERATORS` membership check and the full-meta `fields_for_doctype`
		vocabulary (Task 1/3 stopgaps), both now derived through builder_schema instead."""
		from tatva_connect.automation import describe

		schema = self._builder_schema()
		fields_by_key = {f["key"]: f for f in schema["fields"]}
		for c in self.criteria:
			if not c.field:
				continue
			d = fields_by_key.get(c.field)
			if d is None:
				frappe.throw(
					_("Criterion field {0} is not offered for {1} — it must be a real, enabled Watchable field.").format(
						frappe.bold(c.field), frappe.bold(self.on_doctype)
					),
					title=_("Unknown criterion field"),
				)
			if c.operator:
				allowed_ops = schema["operators_by_type"].get(d["type"], [])
				if c.operator not in allowed_ops:
					frappe.throw(
						_("Operator {0} is not valid for field {1} ({2}).").format(
							frappe.bold(c.operator), frappe.bold(c.field), frappe.bold(d["type"])
						),
						title=_("Operator not valid for field type"),
					)
			if c.operator in _CHANGED_OPERATORS:
				self._validate_changed_operator(c)
				continue
			self._validate_criterion_value(c, d, describe)

	def _validate_criterion_value(self, c, d, describe):
		"""A criterion's value(s) must coerce to its field's schema type (describe.coerces) - the
		value half of the builder contract. Operator-shaped: `is set`/`is not set` carry no value;
		membership splits into items; `is between` checks both bounds. Fail-closed."""
		from tatva_connect.automation import rules

		ftype = d["type"]
		if c.operator in ("is set", "is not set", None, ""):
			return
		if c.operator in ("is one of", "is not one of"):
			values = rules._split_list(c.value)
		elif c.operator == "is between":
			values = [c.from_value, c.value]
		else:
			values = [c.value]
		for v in values:
			if not describe.coerces(v, ftype):
				frappe.throw(
					_("Value {0} does not fit field {1} ({2}).").format(
						frappe.bold(v), frappe.bold(c.field), frappe.bold(ftype)
					),
					title=_("Value does not fit field type"),
				)

	def _validate_changed_operator(self, c):
		"""`changed to` (new value only) and `changed from…to` (old->new pair) are the narrowed-
		transition operators (Part A): valid only when event=Updated (Created has no before-state;
		Deleted fires pre-removal with no diff), and only on a WATCHED field - `router._context_for`
		carries a `{field}__before` key for exactly the fields `router._diff_watched_fields` diffs,
		so a transition on a merely readable field silently never matches. Fail loud at author time."""
		from tatva_connect.automation import fields

		if self.event != "Updated":
			frappe.throw(
				_("Operator {0} is only valid when the rule's Event is Updated.").format(frappe.bold(c.operator)),
				title=_("Operator not valid for event"),
			)
		if not fields.is_watchable(self.on_doctype, c.field):
			frappe.throw(
				_("Operator {0} needs field {1} to be watched — tick Can Watch on its Automation Field row.").format(
					frappe.bold(c.operator), frappe.bold(c.field)
				),
				title=_("Field not watched"),
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
		on the completed task). Mirrors actions._resolve_write_target so a rule can't save a target
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

	def _validate_send_whatsapp(self):
		"""Send WhatsApp: author-time sibling of the send-time guard (sends.template_account_mismatch) -
		catches an obviously wrong pick before save, for the rules whose grain is specific enough to pin
		a single WATI account. A Send WhatsApp action with no template is an incomplete action, same as
		every sibling verb - it throws (R2), never saves silently unconfigured.

		The rule's OWN grain (not a lead's) is resolved ONCE, before the action loop - it depends only
		on this rule's grain, not on any one action - via routing.resolve_account_for_grain, the same
		engine resolve_account_for_lead uses (A.8). Two cases cannot be checked here and degrade to the
		same warn-and-allow path: a partial grain that cannot pin a single account, and an ambiguous tie
		the routing engine itself refuses to resolve (R3) - both let the save through with an orange
		warning; the send-time guard still covers a genuine mismatch per real lead. Only a resolved,
		unambiguous account that differs from a picked template's own account blocks the save."""
		from tatva_connect.automation import sends
		from tatva_connect.whatsapp import routing

		send_whatsapp_actions = [a for a in self.actions if a.action_type == "Send WhatsApp"]
		if not send_whatsapp_actions:
			return

		for a in send_whatsapp_actions:
			if not a.whatsapp_template:
				frappe.throw(
					_("A Send WhatsApp action (row {0}) needs a WhatsApp Template.").format(a.idx),
					title=_("Incomplete action"),
				)

		try:
			account_name = routing.resolve_account_for_grain(self.vertical, self.group, self.program)
		except frappe.ValidationError:
			account_name = None  # an ambiguous tie the routing engine can't resolve - same as unpinnable

		if account_name is None:
			frappe.msgprint(
				_(
					"This rule's grain does not pin a single WhatsApp account, so its Send WhatsApp "
					"template pick(s) cannot be verified now. The send-time guard still blocks a genuine "
					"mismatch for each lead."
				),
				title=_("Cannot verify WhatsApp template account"),
				indicator="orange",
			)
			return

		for a in send_whatsapp_actions:
			mismatch = sends.template_account_mismatch(a.whatsapp_template, account_name)
			if mismatch:
				frappe.throw(
					_("Send WhatsApp action (row {0}): {1}").format(a.idx, mismatch),
					title=_("Template does not match routed account"),
				)

	def on_change(self):
		"""TATVA v2 (Task 4): the wildcard router's `live_doctypes()` guard set is derived from every
		ENABLED rule's on_doctype and cached - the ONE clear point, so an enable/disable/on_doctype
		edit takes effect at once (no per-doctype hook to keep in sync, A.8)."""
		from tatva_connect.automation import router

		router.clear_live_doctypes_cache()

	def on_trash(self):
		"""Same cache-clear as on_change - a deleted rule must drop out of live_doctypes() too - and the
		ONE destructive path for the queue: deleting a rule stops it. Frappe still runs `on_trash` under
		`delete_doc(..., force=1)` (force bypasses the LINK check, not the lifecycle), so the operator
		db-seed's rule re-create can never leave a parked lead pointing at a program nobody will resume."""
		from tatva_connect.automation import router, versions

		router.clear_live_doctypes_cache()
		cancelled = versions.retire(self.name, _("the rule was deleted"))
		if cancelled:
			frappe.msgprint(
				_("{0} parked lead(s) were cancelled with this rule.").format(cancelled),
				title=_("In-flight leads cancelled"),
				indicator="orange",
			)


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
