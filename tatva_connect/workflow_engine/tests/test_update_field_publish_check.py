# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Update Field — item 8 (a typo is refused at PUBLISH) and item 7's control (a clean write still lands).

WHY THIS EXISTS. `Update Field` is one of the two nodes the LeadSquared migration runs on, and its
failure mode was silent: an author typed a word into a date, or a source nobody ever declared, the
workflow published GREEN, and the fault surfaced days later as a dead journey on a real patient's
record with no author anywhere near it. Publish is the last moment that mistake is cheap. This suite
drives `graph.problems` — the same function `CRMWorkflow.publish_problems` delegates to, at
`mode=PUBLISH` with a real graph context — and asserts on the sentences it really returns.

WHAT IS PROVEN, and what each does on the OLD code:

  * a literal the field can never hold is REFUSED, naming the field and the value — OLD: `graph.problems`
    returned nothing at all for a Date row reading "next friday", so the count assertion fails RED.
  * a GOOD literal still publishes — green on both, and it is the control that stops the test above
    from being satisfiable by a check that refuses everything.
  * From Context / Expression / Increment are NOT judged — green on both, because nothing judged
    anything before. Its red is against a WRONG new check: each row is paired with the discriminator
    that the SAME value written as a Literal IS refused, so a check that judged every row would refuse
    every real workflow here and this class would go red rather than silently blessing the regression.
  * a blank literal is not an error — whether a row needs a value is the `reqd` rule's question, and
    answering it twice gives one mistake two messages.
  * the ALLOWLIST refusal is unchanged and is still the reason a forbidden field is refused — one
    fault, one message: a row whose FIELD is already refused is never also blamed for its value.
  * the publish DOOR itself refuses — `CRMWorkflow.assert_publishable()` on a real workflow, so the
    check is proven where an author actually meets it and not only in the function under it.
  * item 7: an ordinary write lands THROUGH `doc.save`, and a later failure in the same segment takes
    it back with it.

Nothing here arms anything. The engine switch stays OFF on purpose: `interpreter.advance` is called
directly, and an armed switch would let the lead's own save start unrelated journeys mid-assertion.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.workflow_engine.tests.test_update_field_publish_check
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import actions, describe
from tatva_connect.tests.authz.grains import assert_masters_exist
from tatva_connect.tests.automation import field_allowlist
from tatva_connect.workflow_engine import graph, interpreter, refs
from tatva_connect.workflow_engine.tests import fixtures as fx

# Four PLAIN parent columns on CRM Lead, one per shape a literal can be wrong in. All four are known
# `lead:*` catalog keys (access/internal_contract_seed.py), so they are parent fields and a parent write
# reaches them; a child-table field would be refused by the allowlist first and prove nothing here.
_DATE = "custom_dob"
_INT = "custom_per_day_opd_size"
_SELECT = "custom_lead_temperature"
_DATA = "first_name"

# The typos. Each is what an author really types, and each is a value its field can NEVER hold.
_NOT_A_DATE = "next friday"
_NOT_A_NUMBER = "a handful"
_NOT_AN_OPTION = "Tepid"


def _row(name, mode, value):
	return {"name": name, "mode": mode, "value": value}


def _declared(fieldname):
	"""What the schema says this field is — asked of the ONE vocabulary the check itself reads."""
	return next(f for f in describe.fields_for_doctype("CRM Lead") if f["key"] == fieldname)


def _label(fieldname):
	"""The field's label as the author sees it, never retyped here."""
	return _declared(fieldname)["label"]


def _an_option(fieldname):
	"""A value the Select really offers — read off its own option lines, so a reseed cannot strand this."""
	return _declared(fieldname)["options"][0]


class _UpdateFieldBase(FrappeTestCase):
	"""The four fields, allowlisted at the grain the Trigger declares, and the publish gate driven whole."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		assert_masters_exist()
		for fieldname in (_DATE, _INT, _SELECT, _DATA):
			field_allowlist.seed_settable(
				"CRM Lead", fieldname,
				vertical=fx.GRAIN["vertical"], group=fx.GRAIN["group"], program=fx.GRAIN["program"],
			)
		cls.lead = fx.make_lead()
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.delete_doc("CRM Lead", cls.lead.name, force=True, ignore_permissions=True)
		field_allowlist.clear("CRM Lead")
		frappe.db.commit()
		super().tearDownClass()

	def _node(self, node_id, node_type, config=None, edges=None):
		"""A node as the DATABASE stores one — `config_json` text and edge ROWS, which is what the Bouncer
		reads. The canvas fixture speaks the authoring shape instead, and mixing the two returns
		`'str' object has no attribute 'get'` rather than a verdict."""
		return {
			"node_id": node_id, "node_type": node_type,
			"config_json": frappe.as_json(config or {}),
			"edges": [{"from_output": o, "to_node": t} for o, t in (edges or {}).items()],
		}

	def _problems(self, *rows):
		"""Every publish fault for an Update Field node carrying `rows`, as the real gate returns them."""
		nodes = [
			self._node("start", "Trigger", {
				"subject_doctype": "CRM Lead", "event": "Created",
				"vertical": fx.GRAIN["vertical"], "group": fx.GRAIN["group"], "program": fx.GRAIN["program"],
			}, {"next": "n1"}),
			self._node("n1", "Update Field",
			           {"target_doctype": "CRM Lead", "updates": list(rows)}, {"next": "end"}),
			self._node("end", "Terminal"),
		]
		return graph.problems(nodes, entry_node="start")

	def _text(self, *rows):
		return " | ".join(p["message"] for p in self._problems(*rows))


class TestALiteralTheFieldCanNeverHoldIsRefusedAtPublish(_UpdateFieldBase):
	"""The headline. RED before: every one of these published green and died on the first live patient.

	The message must carry BOTH halves — the value, so the author can find what they typed, and the
	field, so they know which of ten rows it was in. A sentence with only one of the two sends them
	hunting through the table.
	"""

	def test_a_word_typed_into_a_date_is_refused(self):
		found = self._problems(_row(_DATE, refs.LITERAL, _NOT_A_DATE))
		self.assertEqual(len(found), 1, "a word in a date field still publishes green")
		self.assertIn(_NOT_A_DATE, found[0]["message"])
		self.assertIn(_label(_DATE), found[0]["message"])

	def test_a_word_typed_into_a_whole_number_is_refused(self):
		found = self._problems(_row(_INT, refs.LITERAL, _NOT_A_NUMBER))
		self.assertEqual(len(found), 1, "a word in a number field still publishes green")
		self.assertIn(_NOT_A_NUMBER, found[0]["message"])
		self.assertIn(_label(_INT), found[0]["message"])

	def test_a_value_outside_a_selects_own_list_is_refused(self):
		"""A Select is a fixed list rather than a cast, so it is refused against its OWN option lines —
		the vocabulary the author was offered, never a list retyped in Python."""
		found = self._problems(_row(_SELECT, refs.LITERAL, _NOT_AN_OPTION))
		self.assertEqual(len(found), 1, "a value nobody declared still publishes green")
		self.assertIn(_NOT_AN_OPTION, found[0]["message"])
		self.assertIn(_label(_SELECT), found[0]["message"])

	def test_the_fault_is_anchored_on_the_rows_control_and_on_its_node(self):
		"""The canvas marks a FIELD on a NODE. A sentence with neither can only be shown in a toast, and a
		toast in a graph of twenty nodes is barely better than silence."""
		found = self._problems(_row(_DATE, refs.LITERAL, _NOT_A_DATE))[0]
		self.assertEqual((found["node_id"], found["field"]), ("n1", "updates"))

	def test_one_bad_row_beside_a_good_one_blames_only_itself(self):
		found = self._text(_row(_DATA, refs.LITERAL, "Asha"), _row(_DATE, refs.LITERAL, _NOT_A_DATE))
		self.assertIn(_NOT_A_DATE, found)
		self.assertNotIn("Asha", found, "a good row was blamed for its neighbour")

	def test_two_bad_rows_are_both_reported_at_once(self):
		"""Every fault together — fixing a five-row node one error per publish is five round trips."""
		found = self._problems(
			_row(_DATE, refs.LITERAL, _NOT_A_DATE), _row(_INT, refs.LITERAL, _NOT_A_NUMBER))
		self.assertEqual(len(found), 2)


class TestAGoodLiteralStillPublishes(_UpdateFieldBase):
	"""The control that makes the class above mean something. Green on old code AND on new: without it,
	a check that refused every literal would satisfy every assertion up there."""

	def test_a_real_date_publishes(self):
		self.assertEqual(self._text(_row(_DATE, refs.LITERAL, "1990-04-17")), "")

	def test_a_real_number_publishes(self):
		self.assertEqual(self._text(_row(_INT, refs.LITERAL, "7")), "")

	def test_a_declared_option_publishes(self):
		self.assertEqual(self._text(_row(_SELECT, refs.LITERAL, _an_option(_SELECT))), "")

	def test_free_text_into_a_data_field_publishes(self):
		"""A Data field holds anything, so nothing about a literal can be wrong there — a check that
		judged one would refuse every subject line an author ever writes."""
		self.assertEqual(self._text(_row(_DATA, refs.LITERAL, _NOT_A_DATE)), "")

	def test_all_four_shapes_publish_together(self):
		self.assertEqual(self._text(
			_row(_DATE, refs.LITERAL, "1990-04-17"),
			_row(_INT, refs.LITERAL, "7"),
			_row(_SELECT, refs.LITERAL, _an_option(_SELECT)),
			_row(_DATA, refs.LITERAL, "Asha"),
		), "")


class TestAValueOnlyKnownAtRuntimeIsNotJudged(_UpdateFieldBase):
	"""THE regression this leg could most easily have shipped, and the one that would break every real
	workflow: a From Context row carries a VARIABLE NAME, an Expression row carries CODE, an Increment
	row carries a STEP. None of the three is a value of the target field's type, and judging them as one
	refuses a correct workflow with no way forward — which teaches authors to distrust the gate.

	Each row is paired with its discriminator: the SAME string, written as a Literal, IS refused. So this
	class cannot be passed by a check that judges nothing, and cannot be passed by one that judges
	everything. Green on old code (nothing was judged); red on a new check that over-reached.
	"""

	def test_a_from_context_row_carries_a_variable_name_not_a_date(self):
		reference = f"{refs.slug('CRM Lead')}.{_DATE}"
		self.assertEqual(self._text(_row(_DATE, refs.FROM_CONTEXT, reference)), "",
		                 "publish judged a variable name as if it were the value")
		self.assertIn(reference, self._text(_row(_DATE, refs.LITERAL, reference)))

	def test_an_expression_row_carries_code_not_a_number(self):
		expression = "add_days(nowdate(), 14)"
		self.assertEqual(self._text(_row(_INT, refs.EXPRESSION, expression)), "",
		                 "publish judged an expression as if it were the value")
		self.assertIn(expression, self._text(_row(_INT, refs.LITERAL, expression)))

	def test_an_increment_row_carries_a_step_not_a_value(self):
		"""A step is arithmetic the runtime does against whatever the counter holds; half a point added
		to a whole-number column is the field's own cast to make on save, not a publish fault."""
		step = "2.5"
		self.assertEqual(self._text(_row(_INT, refs.INCREMENT, step)), "",
		                 "publish judged an increment step as if it were the value")
		self.assertIn(step, self._text(_row(_INT, refs.LITERAL, step)))

	def test_a_row_carrying_no_mode_is_judged_as_the_literal_the_runtime_would_write(self):
		"""`contract.resolve_row` treats an absent mode as Literal, so publish must too — judging it as
		un-judgeable would leave the one shape a half-authored node really saves in unchecked."""
		self.assertIn(_NOT_A_DATE, self._text({"name": _DATE, "value": _NOT_A_DATE}))

	def test_a_workflow_mixing_all_three_runtime_modes_publishes(self):
		"""The shape a real journey has. If this goes red, no migrated LeadSquared automation publishes."""
		self.assertEqual(self._text(
			_row(_DATE, refs.FROM_CONTEXT, f"{refs.slug('CRM Lead')}.{_DATE}"),
			_row(_DATA, refs.EXPRESSION, "add_days(nowdate(), 14)"),
			_row(_INT, refs.INCREMENT, "1"),
		), "")


class TestABlankIsNotAnError(_UpdateFieldBase):
	"""Whether a row needs a value at all is the `reqd` rule's question. Answering it here as well gives
	one mistake two messages, and a half-written row is ordinary work rather than a fault."""

	def test_an_empty_literal_is_not_judged(self):
		self.assertEqual(self._text(_row(_DATE, refs.LITERAL, "")), "")

	def test_a_missing_value_is_not_judged(self):
		self.assertEqual(self._text(_row(_DATE, refs.LITERAL, None)), "")

	def test_a_row_with_no_value_key_at_all_is_not_judged(self):
		self.assertEqual(self._text({"name": _DATE, "mode": refs.LITERAL}), "")


class TestTheAllowlistRefusalIsUnchanged(_UpdateFieldBase):
	"""Item 8 must not have moved item 7's gate. A field outside Automation Fields is still refused, and
	still for THAT reason: a row whose field is already refused is never also blamed for its value.

	The discriminator is wording-independent on purpose — the allowlist sentence names the field and the
	doctype, the value sentence always quotes the value back. One problem that does not contain the value
	is the allowlist's, whatever either sentence is later reworded to.
	"""

	def test_a_field_nobody_allowlisted_is_still_refused(self):
		found = self._problems(_row("zz_not_settable", refs.LITERAL, "Asha"))
		self.assertEqual(len(found), 1)
		self.assertIn("zz_not_settable", found[0]["message"])

	def test_a_forbidden_field_with_a_bad_literal_gets_ONE_message_and_it_is_the_allowlists(self):
		found = self._problems(_row("zz_not_settable", refs.LITERAL, _NOT_A_DATE))
		self.assertEqual(len(found), 1, "one fault was reported twice")
		self.assertIn("zz_not_settable", found[0]["message"])
		self.assertNotIn(_NOT_A_DATE, found[0]["message"],
		                 "the row was blamed for its value on top of its field")

	def test_an_allowed_row_beside_a_forbidden_one_is_not_blamed(self):
		found = self._text(
			_row(_DATA, refs.LITERAL, "Asha"), _row("zz_not_settable", refs.LITERAL, "x"))
		self.assertIn("zz_not_settable", found)
		self.assertNotIn(_DATA, found)


class TestThePublishDoorItselfRefuses(_UpdateFieldBase):
	"""Where the author actually meets it. `graph.problems` is the function; `assert_publishable` is the
	door, and a check that only the function knows about protects nobody.

	The node is saved FIRST and saves clean, which is the design and not an accident: a node save is
	authoring (`mode=DRAFT`, no graph context), so a half-typed value must still save. Publish is where
	"later" runs out.
	"""

	_WORKFLOW = "update-field-publish-probe"

	def _workflow_with(self, row):
		fx.purge(self._WORKFLOW)
		self.addCleanup(fx.purge, self._WORKFLOW)
		return fx.make_workflow(self._WORKFLOW, [
			fx.trigger(to="n1"),
			fx.node("n1", "Update Field", edges={"next": "end"}, config={
				"target_doctype": "CRM Lead", "updates": [row],
			}),
			fx.node("end", "Terminal"),
		], lifecycle_state="Draft")

	def test_publishing_a_workflow_with_a_bad_literal_is_refused(self):
		workflow = self._workflow_with(_row(_DATE, refs.LITERAL, _NOT_A_DATE))
		with self.assertRaises(frappe.ValidationError) as raised:
			workflow.assert_publishable()
		self.assertIn(_NOT_A_DATE, str(raised.exception))

	def test_the_same_workflow_with_a_good_literal_publishes(self):
		workflow = self._workflow_with(_row(_DATE, refs.LITERAL, "1990-04-17"))
		workflow.assert_publishable()

	def test_the_refusal_blocks_rather_than_warns(self):
		"""A `warns` (the engine being off) is a fact the author should see, not a reason to refuse. A typo
		is the opposite, and `assert_publishable` counts BLOCKS only — a warning here would publish."""
		from tatva_connect.workflow_engine import registry

		workflow = self._workflow_with(_row(_DATE, refs.LITERAL, _NOT_A_DATE))
		blocking = [p for p in workflow.publish_problems()
		            if p["severity"] == registry.BLOCKS and _NOT_A_DATE in p["message"]]
		self.assertEqual(len(blocking), 1)


class TestANormalUpdateFieldWriteStillLands(_UpdateFieldBase):
	"""Item 7's control — the node this migration runs on, unchanged.

	`doc.save` is the assertion, not an implementation note: `lead_name` is DERIVED from `first_name` by
	`CRMLead.validate`, so a write that lands with the name in step went through the document and its
	hooks. `frappe.db.set_value` would leave the two disagreeing and nothing would say so.
	"""

	def _reread(self, fieldname):
		return frappe.db.get_value("CRM Lead", self.lead.name, fieldname)

	def test_the_write_lands_and_the_records_own_hooks_ran(self):
		actions._action_set_field(
			frappe._dict(action_type="Update Field", target_doctype="CRM Lead",
			             updates=[_row(_DATA, refs.LITERAL, "Asha")]),
			self.lead.name, {}, fx.AXES, None,
		)
		self.assertEqual(self._reread(_DATA), "Asha")
		self.assertEqual(self._reread("lead_name"), "Asha",
		                 "the derived name did not follow, so the write bypassed the document")

	def test_a_later_failure_in_the_same_segment_rolls_the_write_back(self):
		"""Group atomicity, DRIVEN through the real executor rather than argued about. A segment commits
		only at a Wait or a Terminal, so a node that fails after a write takes that write with it — the
		patient's record is never left holding half a journey.

		The second node fails at RUNTIME and not at publish, which is the only honest way to build this:
		`1/0` parses, so the gate has nothing to refuse, and the division happens where a live journey
		would meet it.
		"""
		workflow_name = "update-field-rollback-probe"
		fx.purge(workflow_name)
		self.addCleanup(fx.purge, workflow_name)
		workflow = fx.make_workflow(workflow_name, [
			fx.trigger(to="n1"),
			fx.node("n1", "Update Field", edges={"next": "n2"}, config={
				"target_doctype": "CRM Lead", "updates": [_row(_DATA, refs.LITERAL, "Asha")],
			}),
			fx.node("n2", "Update Field", edges={"next": "end"}, config={
				"target_doctype": "CRM Lead", "updates": [_row(_INT, refs.EXPRESSION, "1/0")],
			}),
			fx.node("end", "Terminal"),
		])
		before = self._reread(_DATA)

		run = fx.start_journey(workflow, self.lead.name, "start")
		interpreter.advance(frappe.get_doc(fx.JOURNEY_DT, run.name))

		self.assertEqual(frappe.db.get_value(fx.JOURNEY_DT, run.name, "status"), "Failed")
		self.assertEqual(self._reread(_DATA), before,
		                 "the first node's write survived a failure two nodes later")
