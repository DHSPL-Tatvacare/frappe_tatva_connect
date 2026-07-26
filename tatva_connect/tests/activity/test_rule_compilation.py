# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Phase 9 of docs/plans/task-form-layer/2026-07-25-task-slots-to-sections-and-form-layer.md — the form reacts.

An admin declares a form's behaviour as FLAT rows on the task type (D9): `When field · operator · value`
→ `action` → `targets`. The server COMPILES those rows into the `depends_on` and `mandatory_depends_on`
strings the two SHIPPED evaluators already read — `utils/expressions.js:evaluateDependsOnValue` on the
browser and `activity/api.py:_field_visible` on the server (D8/D21). There is no third evaluator, no new
client machinery and nothing is written back to the declaration.

What is asserted:

  * the GRAMMAR the compile speaks is the grammar the doctype offers — the Select options and the compile's
    vocabulary are locked to each other, so neither can drift (§17.2 + D27/D28);
  * a Show rule naming many targets reveals every one of them and touches nothing else; a field no rule
    names keeps the condition its own row declares, so a rule-free type is what shipped;
  * a blank When is the form's OPENING state (D25): a field hidden by a blank-When Hide row and shown by a
    condition elsewhere compiles to that condition, and is not cancelled by its own baseline;
  * a conditional Hide overrides a Show on the same field (§17.3);
  * CASCADES settle at the fixpoint (D22/D29): a hidden field's value is INERT, so hiding a parent hides
    whatever hung off its answer — and a value submitted for a hidden field is REFUSED at save;
  * Make Mandatory is enforced only while its condition holds, and never on a hidden field;
  * a rule row naming a field the type does not declare, a target it does not declare, or a value the named
    field does not offer, is refused at save NAMING ITS ROW — the only address a child row has;
  * the compiled string uses only the syntax BOTH evaluators read. The server's is Python `safe_eval`, the
    client's is a JS `new Function`; `and`/`or`/`not` parse in one and `&&`/`||`/`!` in the other, so a
    compiled condition that reached for either would silently evaluate to "visible" on one side.

EXPECTED_RED_TODAY (measured against the pre-batch code, per group):

  * every test calling `compiled_fields` / `_shown_fieldnames` / `RULE_ACTIONS` raises `AttributeError` —
    those names do not exist on `activity.api` yet;
  * every `_refuse` test raises inside `doc.append("rules", ...)` — `CRM Task Type` has no `rules` field, so
    `_init_child` finds no docfield to append into;
  * `test_a_value_submitted_for_a_hidden_field_is_refused` FAILS on the assertion: old code stores the value
    for the hidden field silently and never throws;
  * `test_a_make_mandatory_rule_is_enforced_while_its_condition_holds` FAILS on the assertion: old code has
    no conditional mandatory, so the save succeeds.

Three tests are CONTROLS and pass on old code by design — `test_the_same_value_is_accepted_once_the_rule
_shows_the_field`, `test_the_same_field_is_not_required_when_the_condition_does_not_hold` and
`test_a_hidden_field_made_mandatory_by_a_rule_does_not_block_the_save`. They exist so the refusals above
cannot be satisfied by a build that simply stopped saving or stopped requiring anything.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.activity.test_rule_compilation
"""
import json

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.activity import api as activity_api
from tatva_connect.tests.activity import task_type_fixture

OUTCOME = "zz_rc_outcome"
ORDER_ID = "zz_rc_order_id"
UNITS = "zz_rc_units"
NOTE = "zz_rc_note"
UNTOUCHED = "zz_rc_untouched"
OWN_CONDITION = "zz_rc_own"

GATE = "zz_rc_gate"
MIDDLE = "zz_rc_middle"
LEAF = "zz_rc_leaf"

CONNECTED = "Connected"
NOT_CONNECTED = "Not Connected"

# The three verbs and the four operators are the doctype's Select options; a test may name them because it
# is asserting the compile AGAINST the declaration, which is the one place they live.
_SHOW, _HIDE, _MANDATORY = "Show", "Hide", "Make Mandatory"


def _options(doctype, fieldname):
	"""A Select field's declared options, in order — the doctype JSON is the grammar's one home."""
	df = frappe.get_meta(doctype).get_field(fieldname)
	return tuple(o.strip() for o in (df.options or "").split("\n") if o.strip())


def _by_name(fields):
	return {f.fieldname: f for f in fields}


class TestRuleCompilation(FrappeTestCase):
	"""Two types: one carrying the Nivolumab shape (an onload baseline + reveals), one a three-deep cascade."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")

		# The Nivolumab shape, minimised: the form opens as Outcome only; "Connected" reveals the order
		# block and makes one of it mandatory; "Not Connected" hides a field the reveal had shown.
		cls.reveal_type = task_type_fixture.mint_type(
			"ZZ Rule Compile Reveal Probe",
			[
				{"label": "ZZ Outcome", "fieldname": OUTCOME, "fieldtype": "Select",
				 "options": f"{CONNECTED}\n{NOT_CONNECTED}"},
				{"label": "ZZ Order Id", "fieldname": ORDER_ID, "fieldtype": "Data"},
				{"label": "ZZ Units", "fieldname": UNITS, "fieldtype": "Data"},
				{"label": "ZZ Note", "fieldname": NOTE, "fieldtype": "Data"},
				{"label": "ZZ Untouched", "fieldname": UNTOUCHED, "fieldtype": "Data"},
				{"label": "ZZ Own Condition", "fieldname": OWN_CONDITION, "fieldtype": "Data",
				 "depends_on": f"eval:doc.{OUTCOME}==\"{CONNECTED}\""},
			],
			rules=[
				# Form onload — a row with no When: the opening state.
				{"rule_label": "Form onload", "action": _SHOW, "targets": OUTCOME},
				{"rule_label": "Form onload", "action": _HIDE, "targets": f"{ORDER_ID}, {UNITS}, {NOTE}"},
				{"rule_label": "Connected", "condition_field": OUTCOME, "operator": "is",
				 "condition_value": CONNECTED, "action": _SHOW, "targets": f"{ORDER_ID}, {UNITS}, {NOTE}"},
				{"rule_label": "Connected", "condition_field": OUTCOME, "operator": "is",
				 "condition_value": CONNECTED, "action": _MANDATORY, "targets": ORDER_ID},
				{"rule_label": "Not connected", "condition_field": OUTCOME, "operator": "is",
				 "condition_value": NOT_CONNECTED, "action": _HIDE, "targets": UNITS},
			],
		)

		# Every fixture type is minted HERE: mint_type COMMITS, so a type minted inside a test would survive
		# its rollback and leak into the next one.
		cls.flat_type = task_type_fixture.mint_type("ZZ Rule Compile Flat Probe", [
			{"label": "ZZ Flat", "fieldname": "zz_rc_flat", "fieldtype": "Data"},
		])
		cls.buried_type = task_type_fixture.mint_type("ZZ Rule Compile Buried Probe", [
			{"label": "ZZ Asked", "fieldname": "zz_rc_asked", "fieldtype": "Data"},
			{"label": "ZZ Buried", "fieldname": "zz_rc_buried", "fieldtype": "Data"},
		], rules=[{"rule_label": "Baseline", "action": _HIDE, "targets": "zz_rc_buried"}])
		cls.quoted_type = task_type_fixture.mint_type("ZZ Rule Compile Quote Probe", [
			{"label": "ZZ Q Gate", "fieldname": "zz_rc_q_gate", "fieldtype": "Select",
			 "options": 'He said "yes"'},
			{"label": "ZZ Q Leaf", "fieldname": "zz_rc_q_leaf", "fieldtype": "Data"},
		], rules=[{"rule_label": "Quoted", "condition_field": "zz_rc_q_gate", "operator": "is",
				   "condition_value": 'He said "yes"', "action": _SHOW, "targets": "zz_rc_q_leaf"}])

		# A three-deep chain: the gate decides the middle, the middle decides the leaf.
		cls.cascade_type = task_type_fixture.mint_type(
			"ZZ Rule Compile Cascade Probe",
			[
				{"label": "ZZ Gate", "fieldname": GATE, "fieldtype": "Select", "options": "Yes\nNo"},
				{"label": "ZZ Middle", "fieldname": MIDDLE, "fieldtype": "Select", "options": "Yes\nNo"},
				{"label": "ZZ Leaf", "fieldname": LEAF, "fieldtype": "Data"},
			],
			rules=[
				{"rule_label": "Baseline", "action": _HIDE, "targets": f"{MIDDLE}, {LEAF}"},
				{"rule_label": "Gate yes", "condition_field": GATE, "operator": "is", "condition_value": "Yes",
				 "action": _SHOW, "targets": MIDDLE},
				{"rule_label": "Middle yes", "condition_field": MIDDLE, "operator": "is",
				 "condition_value": "Yes", "action": _SHOW, "targets": LEAF},
				{"rule_label": "Middle yes", "condition_field": MIDDLE, "operator": "is",
				 "condition_value": "Yes", "action": _MANDATORY, "targets": LEAF},
			],
		)

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		task_type_fixture.teardown()
		super().tearDownClass()

	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		frappe.set_user("Administrator")
		self.lead = frappe.get_doc({
			"doctype": "CRM Lead", "first_name": "Rule Compile Probe",
			"mobile_no": f"+9198127{int(frappe.generate_hash(length=8), 16) % 100000:05d}",
			"custom_vertical": task_type_fixture.VERTICAL, "custom_group": task_type_fixture.GROUP,
		}).insert(ignore_permissions=True)

	def _fields(self, task_type):
		return activity_api.compiled_fields(frappe.get_doc("CRM Task Type", task_type))

	def _shown(self, task_type, values):
		return activity_api._shown_fieldnames(self._fields(task_type), values)

	# ---- the grammar is the declaration's, not the code's ---------------------------------------------

	def test_the_rule_grammar_the_compile_speaks_is_the_grammar_the_doctype_offers(self):
		"""One rule, expressed once: the Select options an admin picks from ARE the compile's vocabulary.
		If either side gains a verb or an operator the other has not, this is the test that says so."""
		self.assertEqual(_options("CRM Task Type Rule", "action"), activity_api.RULE_ACTIONS,
						 "the Action options and the compile's verbs disagree")
		self.assertEqual(_options("CRM Task Type Rule", "operator"), activity_api.RULE_OPERATORS,
						 "the Operator options and the compile's operators disagree")
		self.assertIn(activity_api.LEAD_SOURCE, _options("CRM Task Type Field", "source"),
					  "`source` no longer offers the value the lead write path keys off")

	def test_a_rule_free_type_is_exactly_what_shipped(self):
		"""The whole reason Phase 9 is invisible on delivery: no rules, no compiled anything."""
		field = self._fields(self.flat_type)[0]
		self.assertEqual((field.depends_on, field.mandatory_depends_on), ("", ""),
						 "a type declaring no rule grew a compiled condition")

	def test_a_field_no_rule_names_keeps_the_condition_its_own_row_declares(self):
		"""A hand-typed condition is not overwritten by a compile that has nothing to say about the field."""
		field = _by_name(self._fields(self.reveal_type))[OWN_CONDITION]
		self.assertEqual(field.depends_on, f'eval:doc.{OUTCOME}=="{CONNECTED}"',
						 "the compile replaced a condition no rule names")

	# ---- Show / Hide / baseline ------------------------------------------------------------------------

	def test_one_show_rule_reveals_every_target_it_names_and_nothing_else(self):
		"""D9's point: many actions are many rows, and one row names many fields. Three targets, three
		compiled conditions; the field the rules never mention is untouched."""
		fields = _by_name(self._fields(self.reveal_type))
		for fieldname in (ORDER_ID, UNITS, NOTE):
			self.assertIn(f'doc.{OUTCOME}=="{CONNECTED}"', fields[fieldname].depends_on,
						  f"{fieldname} was named by the Show rule but carries no compiled condition")
		self.assertEqual(fields[UNTOUCHED].depends_on, "",
						 "a field no rule names was given a condition")

	def test_the_form_opens_as_the_onload_rows_declare_it(self):
		"""§16.2 step 2 — with nothing answered the order block is CLOSED and only the outcome is asked. The
		blank-When Hide row is the baseline, so the fields it names start closed even though a Show rule
		reveals them later (D25)."""
		shown = self._shown(self.reveal_type, {})
		self.assertIn(OUTCOME, shown, "the onload Show row did not open Outcome")
		self.assertNotIn(ORDER_ID, shown, "the order block was open before an outcome was picked")
		self.assertNotIn(UNITS, shown)
		self.assertNotIn(NOTE, shown)

	def test_picking_the_trigger_value_reveals_exactly_the_rules_targets(self):
		"""The delta that matters: from one shown field to four, and no more than four."""
		before = self._shown(self.reveal_type, {})
		after = self._shown(self.reveal_type, {OUTCOME: CONNECTED})
		self.assertEqual(after - before, {ORDER_ID, UNITS, NOTE, OWN_CONDITION},
						 "picking the trigger revealed a different set from the one the rules name")

	def test_a_conditional_hide_overrides_a_show_on_the_same_field(self):
		"""§17.3's precedence, on the one field both a Show and a Hide row name."""
		self.assertIn(UNITS, self._shown(self.reveal_type, {OUTCOME: CONNECTED}))
		self.assertNotIn(UNITS, self._shown(self.reveal_type, {OUTCOME: NOT_CONNECTED}),
						 "the Hide row did not override the Show row on the same field")

	def test_a_field_named_only_in_a_blank_when_hide_is_never_shown(self):
		"""The baseline with nothing to reopen it. A form may declare a field it never asks."""
		shown = self._shown(self.buried_type, {"zz_rc_buried": ""})
		self.assertEqual(shown, {"zz_rc_asked"}, "a field hidden by the baseline was shown anyway")

	def test_the_compiled_condition_uses_only_syntax_both_evaluators_read(self):
		"""The lock on the compile's output. The server evaluator is Python `safe_eval` and the client's is a
		JS `new Function`: a compiled condition reaching for `and`/`or`/`not` or `&&`/`||`/`!` would raise on
		one side, and both evaluators treat a raised expression as SHOWN — so the rule would silently stop
		hiding on exactly one surface. Every emitted string is comparisons combined arithmetically."""
		forbidden = (" and ", " or ", "not ", "&&", "||", "!")
		for task_type in (self.reveal_type, self.cascade_type):
			for f in self._fields(task_type):
				for expr in (f.depends_on, f.mandatory_depends_on):
					if not expr.startswith("eval:"):
						continue
					for token in forbidden:
						self.assertNotIn(token, expr,
										 f"{f.fieldname} compiled to `{expr}`, which only one evaluator reads")

	def test_the_servers_evaluator_really_evaluates_the_compiled_expression(self):
		"""The sharpest test in this module. `_field_visible` treats an unparseable condition as SHOWN, so a
		compiled expression the server's `safe_eval` could not parse would silently disable every Hide rule
		while every reveal still looked right. Asserted head-on: the SAME expression answers False for one
		set of answers and True for another, which the exception path could not produce."""
		condition = _by_name(self._fields(self.reveal_type))[ORDER_ID].depends_on
		self.assertTrue(condition.startswith("eval:"), "the compile emitted no expression to evaluate")
		self.assertTrue(activity_api._field_visible(condition, {OUTCOME: CONNECTED}),
						"the compiled reveal did not evaluate true on its own trigger")
		self.assertFalse(activity_api._field_visible(condition, {OUTCOME: NOT_CONNECTED}),
						 "the compiled condition was swallowed and answered `shown` for everything")

	def test_a_quoted_value_survives_the_compile(self):
		"""A condition value is admin-typed text. It is JSON-quoted, which both languages read as a literal —
		a bare paste would end the string early and the whole expression would be treated as shown."""
		field = _by_name(self._fields(self.quoted_type))["zz_rc_q_leaf"]
		self.assertIn(json.dumps('He said "yes"'), field.depends_on, "the value was not quoted for the evaluator")
		self.assertIn("zz_rc_q_leaf", self._shown(self.quoted_type, {"zz_rc_q_gate": 'He said "yes"'}),
					  "the quoted condition did not evaluate")

	# ---- cascades, at the fixpoint --------------------------------------------------------------------

	def test_a_hidden_fields_answer_is_inert_all_the_way_down_the_chain(self):
		"""D22 + D29. The rep answered the middle field, then flipped the gate that showed it. The middle is
		hidden, so its answer is INERT — and the leaf that hung off that answer is hidden too. One pass would
		have left the leaf open; the fixpoint closes it."""
		shown = self._shown(self.cascade_type, {GATE: "No", MIDDLE: "Yes", LEAF: "anything"})
		self.assertEqual(shown, {GATE}, "a hidden field's answer still reached through the chain")

	def test_the_chain_opens_fully_when_every_gate_holds(self):
		"""The control. Without it the test above would pass on a build that simply hid everything."""
		shown = self._shown(self.cascade_type, {GATE: "Yes", MIDDLE: "Yes"})
		self.assertEqual(shown, {GATE, MIDDLE, LEAF}, "the chain did not open on the answers that open it")

	# ---- the save honours exactly what the form showed ------------------------------------------------

	def test_a_value_submitted_for_a_hidden_field_is_refused(self):
		"""D22 on the SERVER. A form that never showed a field cannot have collected it, so an answer arriving
		for one is refused rather than stored under a question nobody was asked."""
		with self.assertRaises(frappe.ValidationError) as caught:
			activity_api.save_activity(self.lead.name, self.reveal_type,
									   {OUTCOME: NOT_CONNECTED, UNITS: "12"})
		self.assertIn("ZZ Units", str(caught.exception),
					  "the save was refused by something other than the hidden field's value")

	def test_the_same_value_is_accepted_once_the_rule_shows_the_field(self):
		"""The control for the refusal above, and the proof it is the RULE deciding and not the field."""
		name = activity_api.save_activity(self.lead.name, self.reveal_type,
										 {OUTCOME: CONNECTED, ORDER_ID: "ord-1", UNITS: "12"})
		values = activity_api._task_values(frappe.get_doc("CRM Task", name),
										  activity_api._type_config(self.reveal_type))
		self.assertEqual(values.get(UNITS), "12", "a shown field's answer did not reach its home")

	def test_a_make_mandatory_rule_is_enforced_while_its_condition_holds(self):
		"""§17.3 — mandatory is static `reqd` OR a compiled condition. Nothing on the row is marked reqd."""
		with self.assertRaises(frappe.ValidationError) as caught:
			activity_api.save_activity(self.lead.name, self.reveal_type, {OUTCOME: CONNECTED})
		self.assertIn("ZZ Order Id", str(caught.exception),
					  "the conditional mandatory field did not block the save")

	def test_the_same_field_is_not_required_when_the_condition_does_not_hold(self):
		"""The control: a rule adds a requirement under a condition, never a permanent one."""
		name = activity_api.save_activity(self.lead.name, self.reveal_type, {OUTCOME: NOT_CONNECTED})
		self.assertTrue(frappe.db.exists("CRM Task", name),
						"a conditionally mandatory field blocked a save its condition did not reach")

	def test_a_hidden_field_made_mandatory_by_a_rule_does_not_block_the_save(self):
		"""The two rules meeting. The middle field is answered and then hidden, so the leaf it made mandatory
		is hidden too — and a hidden field is never required, whatever made it mandatory."""
		name = activity_api.save_activity(self.lead.name, self.cascade_type, {GATE: "No"})
		self.assertTrue(frappe.db.exists("CRM Task", name),
						"a requirement survived on a field the form never showed")

	# ---- a bad rule is refused at save, naming its row ------------------------------------------------

	def _refuse(self, rule):
		"""Append one bad rule to a real type and save. Returns (message, the row's own idx)."""
		doc = frappe.get_doc("CRM Task Type", self.reveal_type)
		row = doc.append("rules", rule)
		with self.assertRaises(frappe.ValidationError) as caught:
			doc.save()
		return str(caught.exception), row.idx

	def test_a_rule_naming_an_undeclared_condition_field_is_refused_with_its_row_number(self):
		"""A When field that does not exist is a reaction that can never fire — and nothing on screen says
		so. The row number is the only address a child row has, so the message must carry it."""
		message, idx = self._refuse({"condition_field": "zz_rc_nonexistent", "operator": "is",
									 "condition_value": "x", "action": _SHOW, "targets": NOTE})
		self.assertIn("zz_rc_nonexistent", message)
		self.assertIn(str(idx), message, "the refusal did not name the offending row")

	def test_a_rule_naming_an_undeclared_target_is_refused_with_its_row_number(self):
		"""A target that does not exist is an action pointed at nothing."""
		message, idx = self._refuse({"condition_field": OUTCOME, "operator": "is", "condition_value": CONNECTED,
									 "action": _SHOW, "targets": f"{NOTE}, zz_rc_no_such_target"})
		self.assertIn("zz_rc_no_such_target", message)
		self.assertIn(str(idx), message, "the refusal did not name the offending row")

	def test_a_condition_value_outside_the_named_fields_options_is_refused_with_its_row_number(self):
		"""The commonest real mistake: a value typed the way a person says it, not the way the field spells
		it. It would compile fine and simply never match."""
		message, idx = self._refuse({"condition_field": OUTCOME, "operator": "is", "condition_value": "connected",
									 "action": _SHOW, "targets": NOTE})
		self.assertIn("connected", message)
		self.assertIn(str(idx), message, "the refusal did not name the offending row")

	def test_a_value_free_operator_is_not_judged_against_the_options(self):
		"""`is set` asks only whether an answer exists, so a value left beside it decides nothing and must
		not be refused for failing to be an option."""
		doc = frappe.get_doc("CRM Task Type", self.reveal_type)
		doc.append("rules", {"condition_field": OUTCOME, "operator": "is set", "condition_value": "",
							 "action": _SHOW, "targets": NOTE})
		doc.save()
		self.assertIn(NOTE, self._shown(self.reveal_type, {OUTCOME: CONNECTED}))
