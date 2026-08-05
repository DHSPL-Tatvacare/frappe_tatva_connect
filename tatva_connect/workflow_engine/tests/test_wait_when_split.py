# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A Wait's delay and its instant are two settings, and a journey wakes at the same moment it always did.

ONE field, `expression`, used to be all three of a For Duration's delay, an Event-or-Timeout's timeout and
an Until Time's instant. Because it was one field it could only be one control, and the control it got was
a bare `Data` text box that silently required a Python dict literal: an author who typed `2 minutes` got
`A Wait action's expression must evaluate to a non-empty dict of add_to_date kwargs` — from a LIVE journey,
about a real patient, in words that mean nothing to the person who typed it.

WHAT THIS SUITE ASSERTS IS THE INSTANT, NEVER THE PLUMBING. A delay authored through the new control has to
produce the same `resume_at` the old text produced, an Until Time has to work BOTH ways (a moment on the
calendar and a date the run is carrying — that second one is the whole point of a care journey, and the
first design of this change would have deleted it), and a node still in the old shape has to FAIL LOUDLY
rather than park a patient with no clock and no error.

The park is driven through `interpreter._park` itself rather than through a helper, because the defect
class here is a field the runtime reads by name: a test that called `wait_deadline` directly would pass
while `_park` read a key nobody writes any more.
"""
import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, get_datetime

from tatva_connect.patches import split_wait_when
from tatva_connect.tests.authz.grains import assert_masters_exist
from tatva_connect.workflow_engine import contract, interpreter, refs, registry
from tatva_connect.workflow_engine.tests import fixtures as fx

_WF = "wait-when-split"
_NODE_DT = "CRM Workflow Node"


class _WaitBase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		fx.purge(_WF)
		cls.workflow = fx.make_workflow(_WF, [
			fx.trigger(to="w1"),
			fx.node("w1", "Wait", config={"mode": registry.FOR_DURATION, "duration": '{"minutes": 5}'},
			        edges={"next": "end"}),
			fx.node("end", "Terminal"),
		])
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		fx.purge(_WF)
		frappe.db.commit()

	def _resume_at(self, config, state=None):
		"""The instant `_park` really writes for this Wait config — the runtime's own read, not a helper's."""
		return interpreter.wait_deadline(
			config.get("mode"), interpreter._wait_when(config), state or _state(), base=_BASE,
		)


_BASE = get_datetime("2026-08-05 09:00:00")


def _state():
	"""A journey state a Wait can resolve against — the real resolver's mapping, never a bare dict."""
	return refs.Values({"crm_lead": {"custom_review_date": "2026-09-01 07:30:00"}})


class TestADelayWakesWhenItAlwaysDid(_WaitBase):
	def test_a_delay_authored_through_the_new_control_wakes_at_the_same_instant(self):
		"""The stored form did not change, and this is the proof: what the value+unit control writes is
		byte-for-byte what the old text box demanded, so `wait_resume_at` shifts the same base the same way."""
		config = {"mode": registry.FOR_DURATION, "duration": '{"minutes": 5}'}
		self.assertEqual(self._resume_at(config), add_to_date(_BASE, minutes=5))

	def test_a_month_is_still_a_calendar_month_and_not_thirty_days(self):
		"""Why this control stores `add_to_date` kwargs and not seconds. The fork's own `DurationInput` and
		`formatDuration` both speak seconds, which is why they could not serve here: a month has no fixed
		number of them, and quietly turning `{"months": 1}` into 2,592,000s moves a patient's contact."""
		config = {"mode": registry.FOR_DURATION, "duration": '{"months": 1}'}
		self.assertEqual(self._resume_at(config), add_to_date(_BASE, months=1))
		self.assertNotEqual(self._resume_at(config), add_to_date(_BASE, days=30))

	def test_an_event_or_timeout_reads_the_same_one_field(self):
		"""The timeout leg is a delay, so it is the same setting — one control, not a second spelling."""
		config = {"mode": registry.EVENT_OR_TIMEOUT, "duration": '{"hours": 2}'}
		self.assertEqual(self._resume_at(config), add_to_date(_BASE, hours=2))


class TestAnInstantWorksBothWays(_WaitBase):
	def test_a_moment_picked_from_the_calendar(self):
		config = {"mode": registry.UNTIL_TIME,
		          "until_time": {"mode": refs.LITERAL, "value": "2026-08-10 09:00:00"}}
		self.assertEqual(self._resume_at(config), get_datetime("2026-08-10 09:00:00"))

	def test_a_date_the_run_is_carrying_is_the_patients_own(self):
		"""The case the first design of this change would have deleted. A care journey waits until THIS
		patient's review date, not until a date fixed on the calendar when the workflow was authored."""
		config = {"mode": registry.UNTIL_TIME,
		          "until_time": {"mode": refs.FROM_CONTEXT, "value": "crm_lead.custom_review_date"}}
		self.assertEqual(self._resume_at(config), get_datetime("2026-09-01 07:30:00"))

	def test_an_expression_still_computes(self):
		"""The third mode is what makes the split lossless — anything an author had written still runs."""
		config = {"mode": registry.UNTIL_TIME, "until_time": {
			"mode": refs.EXPRESSION, "value": 'add_days(ctx["crm_lead.custom_review_date"], 3)'}}
		self.assertEqual(self._resume_at(config), get_datetime("2026-09-04 07:30:00"))

	def test_a_quote_in_a_picked_instant_cannot_change_what_runs(self):
		"""`frappe.as_json` does the quoting, so a value carrying a quote cannot close the literal early."""
		written = contract.as_expression({"mode": refs.LITERAL, "value": 'x" + str(1) + "'})
		self.assertEqual(frappe.safe_eval(written, {}, {}), 'x" + str(1) + "')

	def test_the_reference_a_picked_value_names_is_visible_to_the_publish_gate(self):
		"""Otherwise publish would accept an instant reading a value nothing upstream produces, and the
		journey would resolve None and park on nothing."""
		pair = {"mode": refs.FROM_CONTEXT, "value": "crm_lead.custom_review_date"}
		self.assertEqual(contract.value_pair_keys(pair), {"crm_lead.custom_review_date"})
		found = contract.reads_of("Wait", {"mode": registry.UNTIL_TIME, "until_time": pair})
		self.assertIn("crm_lead.custom_review_date", [r["ref"] for r in found])


class TestTheOldShapeFailsLoudlyInsteadOfSilently(_WaitBase):
	def test_a_node_frozen_before_the_split_raises_rather_than_parking_with_no_clock(self):
		"""A frozen `CRM Workflow Version` is immutable by design, so the patch cannot reach it. Reading
		nothing would park the journey Parked with no resume_at and no awaiting_signal — indistinguishable
		from a journey that is legitimately still waiting, and nothing would ever wake it."""
		config = {"mode": registry.FOR_DURATION, "expression": '{"minutes": 5}'}
		with self.assertRaises(interpreter._Permanent) as caught:
			interpreter._wait_when(config)
		self.assertIn("republish", str(caught.exception))

	def test_a_wait_with_nothing_set_is_not_mistaken_for_the_old_shape(self):
		self.assertIsNone(interpreter._wait_when({"mode": registry.FOR_DURATION}))


class TestPublishRefusesADelayThatIsNotOne(_WaitBase):
	def _problems(self, config):
		return [p["message"] for p in registry.validate_node("Wait", config, [], mode=registry.PUBLISH)]

	def test_a_delay_typed_the_way_a_person_writes_one_is_refused_at_publish(self):
		"""The whole defect: this used to publish green and throw at a patient. `2 minutes` is not a
		Python expression at all, so the expression kind reports it; either way the author is told at
		author time rather than the journey failing on a live record."""
		found = self._problems({"mode": registry.FOR_DURATION, "duration": "2 minutes"})
		self.assertTrue(found, "a delay nothing can resolve published green")

	def test_a_unit_add_to_date_does_not_know_is_refused_and_names_the_ones_it_does(self):
		found = self._problems({"mode": registry.FOR_DURATION, "duration": '{"fortnights": 1}'})
		self.assertTrue(any("fortnights" in m for m in found), found)
		self.assertTrue(any("minutes" in m for m in found), "the refusal must say what IS allowed")

	def test_an_empty_delay_is_refused(self):
		found = self._problems({"mode": registry.FOR_DURATION, "duration": "{}"})
		self.assertTrue(any("no time at all" in m for m in found), found)

	def test_a_computed_delay_is_allowed_through_because_only_running_it_could_judge_it(self):
		"""A false block is worse than no block: the author has no way forward."""
		found = self._problems({"mode": registry.FOR_DURATION, "duration": '{"days": ctx["n"]}'})
		self.assertEqual([m for m in found if "length of time" in m], [])

	def test_the_units_are_read_off_add_to_date_and_not_typed_out(self):
		"""A hand-written tuple drifts the day frappe adds a unit, and the control offers what this says."""
		units = registry._delay_units()
		self.assertEqual(set(units), {"years", "months", "weeks", "days", "hours", "minutes", "seconds"})
		self.assertNotIn("as_string", units, "a formatting switch is not a length of time")


class TestTheFoldKeepsAnAuthoredWaitIdentical(FrappeTestCase):
	"""The patch, driven the way migrate drives it — against rows written in the OLD shape.

	Written with `db.set_value` rather than through the controller on purpose: the declaration now refuses
	`expression` outright, so a document cannot be saved in the old shape any more. What the patch meets on
	a real migrate is a ROW that was saved when the declaration still accepted it, and that is what this
	builds. Faithful, and the only shape that can exist.
	"""

	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		fx.purge(_WF)
		cls.workflow = fx.make_workflow(_WF, [
			fx.trigger(to="w1"),
			fx.node("w1", "Wait", config={"mode": registry.FOR_DURATION, "duration": '{"minutes": 5}'},
			        edges={"next": "end"}),
			fx.node("end", "Terminal"),
		])
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		fx.purge(_WF)
		frappe.db.commit()

	def _old_shape(self, config):
		"""Write the node back to how a pre-split site stored it, and return its name."""
		name = frappe.db.get_value(_NODE_DT, {"workflow": self.workflow.name, "node_id": "w1"}, "name")
		frappe.db.set_value(_NODE_DT, name, "config_json", frappe.as_json(config), update_modified=False)
		self.addCleanup(lambda: frappe.db.set_value(
			_NODE_DT, name, "config_json",
			frappe.as_json({"mode": registry.FOR_DURATION, "duration": '{"minutes": 5}'}),
			update_modified=False))
		return name

	def _config(self, name):
		return frappe.parse_json(frappe.db.get_value(_NODE_DT, name, "config_json") or "{}")

	def test_a_delay_moves_across_verbatim(self):
		name = self._old_shape({"mode": registry.FOR_DURATION, "expression": '{"days": 14}'})
		split_wait_when.execute()
		after = self._config(name)
		self.assertEqual(after["duration"], '{"days": 14}')
		self.assertNotIn("expression", after)

	def test_an_instant_keeps_running_exactly_what_the_author_wrote(self):
		"""Folded to Expression rather than parsed into a friendlier mode: reading an author's expression
		to decide what they meant is a guess, and a guess here changes when a patient is contacted."""
		written = 'add_days(ctx["crm_lead.custom_review_date"], 3)'
		name = self._old_shape({"mode": registry.UNTIL_TIME, "expression": written})
		split_wait_when.execute()
		after = self._config(name)
		self.assertEqual(after["until_time"], {"mode": refs.EXPRESSION, "value": written})
		self.assertEqual(contract.as_expression(after["until_time"]), written)

	def test_the_folded_node_wakes_at_the_instant_the_old_one_did(self):
		"""The assertion that matters: not the shape, the moment."""
		written = '{"days": 14}'
		name = self._old_shape({"mode": registry.FOR_DURATION, "expression": written})
		before = interpreter.wait_deadline(registry.FOR_DURATION, written, _state(), base=_BASE)
		split_wait_when.execute()
		after = self._config(name)
		self.assertEqual(
			interpreter.wait_deadline(after["mode"], interpreter._wait_when(after), _state(), base=_BASE),
			before,
		)

	def test_it_is_a_free_no_op_the_second_time(self):
		name = self._old_shape({"mode": registry.FOR_DURATION, "expression": '{"days": 14}'})
		split_wait_when.execute()
		once = self._config(name)
		split_wait_when.execute()
		self.assertEqual(self._config(name), once)

	def test_it_leaves_a_node_that_is_already_split_alone(self):
		name = self._old_shape({"mode": registry.FOR_DURATION, "duration": '{"hours": 3}'})
		split_wait_when.execute()
		self.assertEqual(self._config(name)["duration"], '{"hours": 3}')
