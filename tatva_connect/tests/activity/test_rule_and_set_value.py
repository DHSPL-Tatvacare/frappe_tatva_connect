# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Two verbs LeadSquared uses that the compile did not: a rule that ANDs two conditions, and Set Value.

Both are transcribed from live LSQ forms, not invented — `docs/plans/task-form-layer/ANAYA-SEED-INVENTORY.md`:

  * **AND** — Welcome Call (event 230) rules 3, 5, 9 and 14 carry two or three conditions under `If All`.
    Rule 14 is the smallest: `First Session is No` AND `Chemo Status is Chemo Completed Documents Not
    Uploaded` -> make `Followup Date Time` mandatory. Written as two rows the compile ORs them (`_rule_or`
    is `+`), so the field goes mandatory when EITHER holds and a rep is blocked on a form LSQ never asked.
  * **Set Value** — Document Upload (250) rules 2-4, Welcome Call (230) rules 7-8, Order Delivery Status
    (236) rule 1, Order Punch Status (235) rule 5. Every one reads `Set Value (Mail Merge <- <field>)`: LSQ
    COPIES one field into another when a condition holds, and marks the target Make Read-Only in the same
    rule set. So the verb is a copy and its target is derived, which is what makes it the server's answer
    rather than the client's.

The shapes they compile to are the ones already shipped, not new machinery. `depends_on` says when a field
is SHOWN and `mandatory_depends_on` when it is REQUIRED; `copy_from` carries the Set Value rows naming a
field as `[{source, when}]` — a list because several rows may name one field, and the one that FIRES is the
one whose value is taken. One evaluator throughout, no third grammar.

`*` for AND is forced by the same constraint `_rule_atom` documents for `+`: the string is read by Python
`safe_eval` on the server and a JS `new Function` on the browser, so only arithmetic reads alike in both.

EXPECTED_RED_TODAY, against the pre-change code:
  * every test naming `condition_field_2` / `set_value` fails inside `mint_type` — `CRM Task Type Rule` has
    no such columns, so `_init_child` drops them and the compile never sees them;
  * `test_two_conditions_compile_to_a_product` and `test_set_value_compiles_beside_its_condition` fail on
    the assertion — the compiled string carries only the first condition and no `set_value` at all;
  * `test_the_grammar_is_the_declaration` fails — `Set Value` is not in the doctype's action options.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.activity.test_rule_and_set_value
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.activity import api as activity_api
from tatva_connect.tests.activity import task_type_fixture

FIRST_SESSION = "zz_av_first_session"
CHEMO_STATUS = "zz_av_chemo_status"
FOLLOWUP = "zz_av_followup"
OUTCOME = "zz_av_outcome"
ORDER_STATUS = "zz_av_order_status"
LEAD_FIELD = "first_name"
MARKER = "zz_av_break"

NO, YES = "No", "Yes"
NOT_UPLOADED = "Chemo Completed Documents Not Uploaded"
UPLOADED = "Chemo Completed Documents Uploaded"


def _by_name(fields):
	return {f.fieldname: f for f in fields}


def _options(doctype, fieldname):
	df = frappe.get_meta(doctype).get_field(fieldname)
	return tuple(o.strip() for o in (df.options or "").split("\n") if o.strip())


class TestRuleAndSetValue(FrappeTestCase):
	"""One type carrying LSQ Welcome Call rule 14 verbatim, plus a Set Value in the shape 235 rule 5 uses."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.task_type = task_type_fixture.mint_type(
			"ZZ And Set Value Probe",
			[
				{"label": "ZZ First Session", "fieldname": FIRST_SESSION, "fieldtype": "Select",
				 "options": f"{YES}\n{NO}"},
				{"label": "ZZ Chemo Status", "fieldname": CHEMO_STATUS, "fieldtype": "Select",
				 "options": f"{UPLOADED}\n{NOT_UPLOADED}"},
				{"label": "ZZ Followup", "fieldname": FOLLOWUP, "fieldtype": "Data"},
				{"label": "ZZ Outcome", "fieldname": OUTCOME, "fieldtype": "Select",
				 "options": f"{YES}\n{NO}"},
				{"label": "ZZ Order Status", "fieldname": ORDER_STATUS, "fieldtype": "Data"},
				{"label": "ZZ Patient", "fieldname": LEAD_FIELD, "fieldtype": "Data", "source": "Lead"},
				{"label": "ZZ Break", "fieldname": MARKER, "fieldtype": "Section Break"},
			],
			rules=[
				# LSQ Welcome Call rule 14, verbatim: two conditions under `If All`.
				{"rule_label": "First Session No and Chemo status",
				 "condition_field": FIRST_SESSION, "operator": "is", "condition_value": NO,
				 "condition_field_2": CHEMO_STATUS, "operator_2": "is", "condition_value_2": NOT_UPLOADED,
				 "action": "Make Mandatory", "targets": FOLLOWUP},
				# LSQ 250 rules 2-4 shape: `X is defined -> copy X into its twin`.
				{"rule_label": "Copy the chemo status across",
				 "condition_field": OUTCOME, "operator": "is", "condition_value": YES,
				 "action": "Set Value", "targets": ORDER_STATUS, "set_value": CHEMO_STATUS},
				# A SECOND copy onto the same field, which is what T-06 was about.
				{"rule_label": "Copy the followup across instead",
				 "condition_field": FIRST_SESSION, "operator": "is", "condition_value": YES,
				 "action": "Set Value", "targets": ORDER_STATUS, "set_value": FOLLOWUP},
			],
		)
		cls.lead = frappe.get_doc({
			"doctype": "CRM Lead", "first_name": "And Set Value Probe",
			"mobile_no": f"+9198127{int(frappe.generate_hash(length=8), 16) % 100000:05d}",
			"custom_vertical": task_type_fixture.VERTICAL, "custom_group": task_type_fixture.GROUP,
		}).insert(ignore_permissions=True)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		task_type_fixture.teardown()
		super().tearDownClass()

	def _fields(self):
		return _by_name(activity_api.compiled_fields(frappe.get_doc("CRM Task Type", self.task_type)))

	# ---- the grammar is the declaration ---------------------------------------------------------

	def test_the_grammar_is_the_declaration(self):
		"""The verbs and operators the compile speaks are the doctype's own Select options — one home."""
		self.assertEqual(_options("CRM Task Type Rule", "action"), activity_api.RULE_ACTIONS)
		self.assertEqual(_options("CRM Task Type Rule", "operator"), activity_api.RULE_OPERATORS)
		self.assertEqual(_options("CRM Task Type Rule", "operator_2"), activity_api.RULE_OPERATORS)
		self.assertIn(activity_api.RULE_SET_VALUE, activity_api.RULE_ACTIONS)

	# ---- AND ------------------------------------------------------------------------------------

	def test_two_conditions_compile_to_a_product(self):
		"""`*` is AND, and it is the only AND both evaluators read — `and` is Python-only, `&&` JS-only."""
		expr = self._fields()[FOLLOWUP].mandatory_depends_on
		self.assertEqual(
			expr,
			f'eval:((doc.{FIRST_SESSION}=="{NO}")*(doc.{CHEMO_STATUS}=="{NOT_UPLOADED}"))')
		self.assertNotIn(" and ", expr)
		self.assertNotIn("&&", expr)

	def test_both_conditions_must_hold(self):
		"""The whole point: EITHER alone is not enough. This is what two OR'd rows got wrong."""
		expr = self._fields()[FOLLOWUP].mandatory_depends_on
		both = {FIRST_SESSION: NO, CHEMO_STATUS: NOT_UPLOADED}
		self.assertTrue(activity_api._field_visible(expr, both))
		self.assertFalse(activity_api._field_visible(expr, {FIRST_SESSION: NO, CHEMO_STATUS: UPLOADED}))
		self.assertFalse(activity_api._field_visible(expr, {FIRST_SESSION: YES, CHEMO_STATUS: NOT_UPLOADED}))
		self.assertFalse(activity_api._field_visible(expr, {}))

	def test_a_condition_written_only_in_the_second_slot_still_conditions(self):
		"""T-04: `conditional` decides baseline-vs-branch, so reading one triplet buries the field for good.

		A blank-When Hide is the form's OPENING state (D25) and is deliberately excluded from the negation.
		A row whose only condition sits in the second slot IS conditional, and misreading it as the baseline
		compiles the field to `eval:0` — hidden on every answer, with no way back."""
		doc = frappe.get_doc("CRM Task Type", self.task_type)
		doc.append("rules", {"condition_field_2": OUTCOME, "operator_2": "is", "condition_value_2": YES,
							 "action": "Hide", "targets": FOLLOWUP})
		doc.save()
		try:
			expr = _by_name(activity_api.compiled_fields(doc))[FOLLOWUP].depends_on
			self.assertNotEqual(expr, "eval:0", "the second-slot Hide was read as the blank-When baseline")
			self.assertTrue(activity_api._field_visible(expr, {OUTCOME: NO}), "hidden when its condition is false")
			self.assertFalse(activity_api._field_visible(expr, {OUTCOME: YES}), "shown when its condition is true")
		finally:
			doc.rules = doc.rules[:-1]
			doc.save()

	def test_a_single_condition_row_is_unchanged(self):
		"""A row leaving the second triplet blank compiles exactly as it always did — no stray factor."""
		self.assertEqual(self._fields()[ORDER_STATUS].copy_from[0]["when"], f'eval:doc.{OUTCOME}=="{YES}"')

	# ---- Set Value: a COPY, not a literal ---------------------------------------------------------

	def test_set_value_compiles_to_a_copy_of_a_declared_field(self):
		"""Every Set Value row LSQ declares reads `Mail Merge <- <field>`, so the value is a SOURCE FIELDNAME
		and the row compiles to a copy. A literal served none of the seven real cases."""
		rules = self._fields()[ORDER_STATUS].copy_from
		self.assertEqual([r["source"] for r in rules], [CHEMO_STATUS, FOLLOWUP])
		self.assertEqual(rules[0]["when"], f'eval:doc.{OUTCOME}=="{YES}"')

	def test_the_rule_that_fires_is_the_rule_whose_value_is_copied(self):
		"""T-06. Two copies name one field; the SECOND one's condition holds. Compiling every condition into
		one OR and keeping the first row's source wrote the wrong field's answer under a condition that was
		never about it."""
		fields = activity_api.compiled_fields(frappe.get_doc("CRM Task Type", self.task_type))
		copied = activity_api.copied_values(fields, {
			OUTCOME: NO, FIRST_SESSION: YES, CHEMO_STATUS: UPLOADED, FOLLOWUP: "the followup answer"})
		self.assertEqual(copied.get(ORDER_STATUS), "the followup answer")

	def test_a_copy_reads_the_settled_answers_not_the_stale_ones(self):
		"""T-06/T-10 together: the FIRST rule's condition holds, so its source is the one read."""
		fields = activity_api.compiled_fields(frappe.get_doc("CRM Task Type", self.task_type))
		copied = activity_api.copied_values(fields, {
			OUTCOME: YES, FIRST_SESSION: NO, CHEMO_STATUS: UPLOADED, FOLLOWUP: "not this one"})
		self.assertEqual(copied.get(ORDER_STATUS), UPLOADED)

	def test_no_rule_firing_copies_nothing(self):
		"""A field whose Set Value rules all fail keeps whatever it held — a copy is not a blanking."""
		fields = activity_api.compiled_fields(frappe.get_doc("CRM Task Type", self.task_type))
		self.assertEqual(activity_api.copied_values(fields, {OUTCOME: NO, FIRST_SESSION: NO}), {})

	def test_a_firing_copy_overrides_what_the_client_sent(self):
		"""T-07 and T-08, which are one property: the target is DERIVED. Both save paths run this, so a rep's
		form, the partner API and the migration store the same record from the same declaration — and a
		client asserting its own value for a derived field cannot make it stick."""
		task = activity_api.save_activity(self.lead.name, self.task_type, {
			OUTCOME: YES, CHEMO_STATUS: UPLOADED, FIRST_SESSION: NO,
			ORDER_STATUS: "what the client tried to assert"})
		values = activity_api.task_detail(task)["task"]["values"]
		self.assertEqual(values.get(ORDER_STATUS), UPLOADED)

	def test_a_copy_that_does_not_fire_leaves_the_submitted_value_alone(self):
		"""The other branch, asserted so the claim above is not read wider than it is: the verb OVERRIDES
		where a rule fires, and is silent where none does. A Set Value rule is conditional, so a field whose
		conditions are all false is an ordinary answer and blanking it would discard a real one."""
		task = activity_api.save_activity(self.lead.name, self.task_type, {
			OUTCOME: NO, FIRST_SESSION: NO, CHEMO_STATUS: UPLOADED, ORDER_STATUS: "no rule fires here"})
		values = activity_api.task_detail(task)["task"]["values"]
		self.assertEqual(values.get(ORDER_STATUS), "no rule fires here")

	def test_a_copy_chain_resolves_all_the_way_down(self):
		"""A copy is an answer, so it can feed the next copy. Resolving one pass stored the end of the chain
		BLANK on the server while the browser — which re-runs on its own reactivity — converged and showed
		the rep a value: the same declaration, two different records."""
		chain = task_type_fixture.mint_type(
			"ZZ Copy Chain Probe",
			({"label": "A", "fieldname": "zz_a", "fieldtype": "Data"},
			 {"label": "B", "fieldname": "zz_b", "fieldtype": "Data"},
			 {"label": "C", "fieldname": "zz_c", "fieldtype": "Data"}),
			rules=({"rule_label": "b from a", "action": "Set Value", "targets": "zz_b", "set_value": "zz_a"},
				   {"rule_label": "c from b", "action": "Set Value", "targets": "zz_c", "set_value": "zz_b"}))
		fields = activity_api.compiled_fields(frappe.get_doc("CRM Task Type", chain))
		self.assertEqual(activity_api.copied_values(fields, {"zz_a": "X"}), {"zz_b": "X", "zz_c": "X"})

	def test_a_copy_that_runs_in_a_circle_is_refused(self):
		"""A cycle has no fixpoint: the settle stops on an arbitrary parity, so a no-op re-save mutates the
		record, and the browser's reactive re-run never converges at all."""
		with self.assertRaises(frappe.ValidationError) as caught:
			task_type_fixture.mint_type(
				"ZZ Copy Cycle Probe",
				({"label": "A", "fieldname": "zz_a", "fieldtype": "Data"},
				 {"label": "B", "fieldname": "zz_b", "fieldtype": "Data"}),
				rules=({"rule_label": "b from a", "action": "Set Value",
						"targets": "zz_b", "set_value": "zz_a"},
					   {"rule_label": "a from b", "action": "Set Value",
						"targets": "zz_a", "set_value": "zz_b"}))
		self.assertIn("circle", str(caught.exception).lower())

	def test_a_copy_onto_a_lead_answered_field_is_refused(self):
		"""`compute_activity` takes a lead-sourced field from the LEAD, so a copy would show the rep one
		value in a read-only box and store a different one."""
		self._refuse({"condition_field": OUTCOME, "operator": "is", "condition_value": YES,
					  "action": "Set Value", "targets": LEAD_FIELD, "set_value": CHEMO_STATUS},
					 LEAD_FIELD)

	def test_a_copy_onto_a_layout_row_is_refused(self):
		"""A Section Break is a legitimate Show/Hide target and holds nothing to write, so a copy aimed at
		one reads as configured in the grid and silently does nothing."""
		self._refuse({"condition_field": OUTCOME, "operator": "is", "condition_value": YES,
					  "action": "Set Value", "targets": MARKER, "set_value": CHEMO_STATUS},
					 MARKER)

	def test_a_copy_target_is_read_only(self):
		"""The rep is never offered a keystroke the save throws away. LSQ marks these targets Make Read-Only
		in the very rule sets the copies are transcribed from."""
		descriptors = activity_api._type_config(self.task_type)["fields"]
		self.assertEqual(_by_name(descriptors)[ORDER_STATUS].get("read_only"), 1)
		self.assertFalse(_by_name(descriptors)[FOLLOWUP].get("read_only"))

	def test_set_value_does_not_touch_visibility_or_mandatory(self):
		"""A Set Value rule fills a field; it does not reveal it and does not demand it."""
		f = self._fields()[ORDER_STATUS]
		self.assertFalse(f.depends_on)
		self.assertFalse(f.mandatory_depends_on)

	def test_a_field_no_set_value_rule_names_carries_none(self):
		self.assertEqual(self._fields()[FOLLOWUP].copy_from, [])

	# ---- the validator ---------------------------------------------------------------------------

	def _refuse(self, rule, fragment):
		doc = frappe.get_doc("CRM Task Type", self.task_type)
		doc.append("rules", rule)
		with self.assertRaises(frappe.ValidationError) as caught:
			doc.save()
		self.assertIn(fragment, str(caught.exception))

	def test_an_unknown_second_condition_field_is_refused(self):
		"""The second triplet is checked exactly as the first — a typo there was silent before."""
		self._refuse({"condition_field": FIRST_SESSION, "operator": "is", "condition_value": NO,
					  "condition_field_2": "zz_av_not_a_field", "operator_2": "is", "condition_value_2": YES,
					  "action": "Show", "targets": FOLLOWUP},
					 "zz_av_not_a_field")

	def test_an_unknown_second_condition_value_is_refused(self):
		self._refuse({"condition_field": FIRST_SESSION, "operator": "is", "condition_value": NO,
					  "condition_field_2": CHEMO_STATUS, "operator_2": "is",
					  "condition_value_2": "Nope Not An Option",
					  "action": "Show", "targets": FOLLOWUP},
					 "Nope Not An Option")

	def test_a_set_value_rule_naming_no_source_is_refused(self):
		"""Set Value with nothing to copy from would blank the field it names."""
		self._refuse({"condition_field": OUTCOME, "operator": "is", "condition_value": YES,
					  "action": "Set Value", "targets": ORDER_STATUS},
					 ORDER_STATUS)

	def test_a_set_value_source_the_type_does_not_declare_is_refused(self):
		"""T-09: the source is a fieldname now, so it is checked the way a When field is — the literal it
		replaced was checked for non-blankness and nothing else."""
		self._refuse({"condition_field": OUTCOME, "operator": "is", "condition_value": YES,
					  "action": "Set Value", "targets": ORDER_STATUS, "set_value": "zz_av_not_a_field"},
					 "zz_av_not_a_field")

	def test_a_set_value_copying_a_field_onto_itself_is_refused(self):
		"""It would read as working in the grid and do nothing at all."""
		self._refuse({"condition_field": OUTCOME, "operator": "is", "condition_value": YES,
					  "action": "Set Value", "targets": ORDER_STATUS, "set_value": ORDER_STATUS},
					 ORDER_STATUS)
