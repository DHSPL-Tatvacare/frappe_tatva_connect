# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""W8.4 — "Only once per patient": one checkbox on the Trigger, one question asked at the one door.

WHY IT EXISTS. A cohort re-selects everyone its criteria match, every time it runs. A welcome journey on a
monthly schedule therefore sends the welcome every month, for ever, to the same patient. Nothing in the
engine remembered that this workflow had already run for this lead.

ONCE PER WORKFLOW, EVER — ANY VERSION COUNTS. A version is an EDIT; a workflow is an IDENTITY. Keying on
the version was rejected outright: a one-word typo fix republishes, mints a version, and re-admits every
lead who ever finished, so a spelling correction re-messages a whole cohort.

WHAT COUNTS AS ALREADY RAN, and the two exclusions are the interesting part:
  * `Done`    — yes. The patient received the journey.
  * `Failed`  — NO. The engine broke; excluding a patient for OUR bug punishes them for it. A journey that
                failed at step 8 of 10 will re-run and resend those 8, and that is the accepted cost: a
                duplicate message is visible and complainable, a silently skipped patient is not.
  * `Stopped` — NO. An operator suspended the workflow. Otherwise one Suspend click permanently bars
                everyone who was in flight — and W10 just made Suspend a single button.

NOT BUILT ON `active_key`. That key is CLEARED when a journey ends, deliberately, so the lead is free to
start again — W10's kill depends on it. Run-once is a different question asked at start time. Conflating
the two would break Suspend, so `test_it_does_not_read_the_unique_key` holds them apart.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.workflow_engine.tests.test_run_once_per_lead
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.workflow_engine import drain, interpreter, registry, triggers, versions
from tatva_connect.workflow_engine.tests import fixtures as fx

JOURNEY_DT = fx.JOURNEY_DT
_WF = "ZZ Run Once Per Lead"
_MARKER = "ZZ RunOnce Probe"


class _RunOnceCase(FrappeTestCase):
	"""A scheduled workflow whose criteria select only this suite's leads, so a cohort walk is isolated."""

	ONCE = 1

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.workflow_name = f"{_WF} {cls.__name__}"
		cls.marker = f"{_MARKER} {cls.__name__}"
		fx.purge(cls.workflow_name)
		cls.addClassCleanup(fx.purge, cls.workflow_name)
		fx.arm_engine(True, cls=cls)
		cls.workflow = fx.make_workflow(cls.workflow_name, [
			fx.node("start", "Trigger", config={
				"mode": registry.MODE_SCHEDULE, "subject_doctype": "CRM Lead",
				"schedule": "Daily", "schedule_time": "09:00",
				"vertical": fx.GRAIN["vertical"], "group": fx.GRAIN["group"], "program": fx.GRAIN["program"],
				# The marker is what isolates this suite's cohort — a cohort correctly takes everyone its
				# criteria match, so the criteria have to do the isolating, not the teardown order.
				"predicate": {"type": "rule", "field": "crm_lead.first_name", "operator": "is",
				              "value": cls.marker},
				"once_per_subject": cls.ONCE,
			}, edges={"next": "end"}),
			fx.node("end", "Terminal"),
		])
		frappe.db.commit()

	def setUp(self):
		self.addCleanup(self._purge_journeys)
		self.lead = self._lead()
		self.version = versions.current_name(self.workflow_name)

	def _lead(self):
		"""A probe lead this test owns and destroys. `force` because an enabled Assignment Rule writes a
		CRM Notification against a new lead, and frappe's link check then refuses an ordinary delete."""
		lead = fx.make_lead(first_name=self.marker)
		self.addCleanup(self._drop_lead, lead.name)
		return lead

	def _drop_lead(self, name):
		if frappe.db.exists("CRM Lead", name):
			frappe.delete_doc("CRM Lead", name, force=True, ignore_permissions=True)
			frappe.db.commit()

	def _purge_journeys(self):
		for name in frappe.get_all(JOURNEY_DT, filters={"workflow": self.workflow_name}, pluck="name"):
			frappe.db.delete(fx.STEP_LOG_DT, {"journey": name})
		frappe.db.delete(JOURNEY_DT, {"workflow": self.workflow_name})
		frappe.db.commit()

	def _ended_as(self, status, lead_name=None):
		"""A journey of this workflow that has already ENDED for this lead, in the given terminal state.

		`active_key` is cleared exactly as the engine clears it on every terminal transition — so a check
		built on that key would see nothing here, which is what `test_it_does_not_read_the_unique_key`
		turns into an assertion.
		"""
		journey = fx.start_journey(self.workflow, lead_name or self.lead.name, "end")
		frappe.db.set_value(JOURNEY_DT, journey.name, {
			"status": status, "active_key": None,
		}, update_modified=False)
		frappe.db.commit()
		return journey

	def _journeys(self, lead_name=None):
		return frappe.get_all(
			JOURNEY_DT,
			filters={"workflow": self.workflow_name, "subject_name": lead_name or self.lead.name},
			pluck="name",
		)

	def _start(self):
		return triggers.start_journey(self.workflow_name, self.version, self.lead.name)


class TestOnlyOncePerPatient(_RunOnceCase):
	ONCE = 1

	def test_a_lead_who_completed_it_is_refused(self):
		"""The whole feature: the second enrolment does not happen, and no journey row is born."""
		done = self._ended_as("Done")

		self._start()

		self.assertEqual(self._journeys(), [done.name], "a second journey was started for a completed lead")

	def test_the_refusal_says_when_they_completed_it(self):
		"""Silently absent from a cohort is unanswerable. "Why didn't this patient get it?" is the question
		that actually gets asked, and the completion date is the answer to it."""
		self._ended_as("Done")

		refusal = self._start()

		self.assertTrue(refusal, "the refusal must be a reason, not a bare None")
		self.assertIn("already", refusal.lower())
		self.assertIn(frappe.utils.formatdate(frappe.utils.nowdate()), refusal)

	def test_a_lead_with_no_history_starts_normally(self):
		"""The toggle must not become a gate on everybody."""
		self.assertIsNone(self._start(), "a first-time lead was refused")
		self.assertTrue(self._journeys(), "no journey was started for a lead who has never run this")

	def test_a_journey_that_FAILED_does_not_bar_the_lead(self):
		"""The engine broke. Excluding the patient for our bug punishes them for it."""
		self._ended_as("Failed")

		self.assertIsNone(self._start(), "a lead was barred because the ENGINE failed")
		self.assertEqual(len(self._journeys()), 2)

	def test_a_journey_STOPPED_BY_A_SUSPEND_does_not_bar_the_lead(self):
		"""An operator action is not a patient outcome. Otherwise one Suspend click bars everyone who was
		in flight, permanently — and W10 made Suspend a single button with a confirm."""
		self._ended_as(interpreter.STOPPED)

		self.assertIsNone(self._start(), "a lead was barred because an operator suspended the workflow")
		self.assertEqual(len(self._journeys()), 2)

	def test_a_journey_still_RUNNING_does_not_bar_the_lead(self):
		"""Run-once asks about a COMPLETED journey. A live one is the `active_key` index's question, and
		these two must not be conflated — that key is what W10's kill frees."""
		live = fx.start_journey(self.workflow, self.lead.name, "end")

		self.assertIsNone(self._start(), "an in-flight journey was read as 'already ran'")
		self.assertIn(live.name, self._journeys())

	def test_it_does_not_read_the_unique_key(self):
		"""THE trap. `active_key` is NULL on every ended journey — that is how W10 frees a lead to enter
		again. A run-once built on it would answer "never ran" for every completed lead, silently."""
		done = self._ended_as("Done")
		self.assertIsNone(
			frappe.db.get_value(JOURNEY_DT, done.name, "active_key"),
			"the fixture must have a cleared key, or this proves nothing",
		)

		self.assertTrue(self._start(), "the check read the unique key instead of the terminal status")

	def test_any_version_counts(self):
		"""A version is an EDIT; a workflow is an IDENTITY. A typo fix mints a version, and if that
		re-admitted everyone who had finished, a spelling correction would re-message the whole cohort."""
		self._ended_as("Done")
		first = self.version
		workflow = frappe.get_doc(fx.WORKFLOW_DT, self.workflow_name)
		node = frappe.get_doc(fx.NODE_DT, frappe.get_all(
			fx.NODE_DT, filters={"workflow": self.workflow_name, "node_id": "start"}, pluck="name",
		)[0])
		# A REAL edit through the node's own validator — the sort an author makes and republishes. The rest
		# of the config is carried through, `once_per_subject` included: this is a typo fix, not a redesign.
		config = registry.config_of(node)
		config["schedule_time"] = "10:00"
		node.config_json = frappe.as_json(config)
		node.save(ignore_permissions=True)
		self.version = versions.ensure_version(workflow)
		self.assertNotEqual(self.version, first, "the fixture must mint a NEW version, or this proves nothing")

		self.assertTrue(self._start(), "a new version re-admitted a lead who had already completed it")

	def test_the_cohort_drain_refuses_them_too(self):
		"""THE path it exists for. The save lane starts one lead; the drain re-selects the whole cohort on
		every tick, which is where "for ever, every month" actually comes from."""
		completed = self._lead()
		self._ended_as("Done", lead_name=completed.name)
		fresh = self._lead()

		started = drain.run_cohort(self.workflow_name, respect_switch=False)

		self.assertEqual(len(self._journeys(completed.name)), 1, "the drain re-enrolled a completed lead")
		self.assertTrue(self._journeys(fresh.name), "the drain skipped a lead who had never run it")
		self.assertEqual(
			started, len(self._journeys(fresh.name)) + len(self._journeys(self.lead.name)),
			"the drain counted a refused lead as started — the receipt says the opposite of what happened",
		)


class TestTheToggleIsOffByDefault(_RunOnceCase):
	ONCE = 0

	def test_without_the_toggle_a_completed_lead_starts_again(self):
		"""The control. Every existing workflow keeps its behaviour exactly — the toggle adds a rule, it
		does not change the default one."""
		self._ended_as("Done")

		self.assertIsNone(self._start(), "the check fired on a workflow that never asked for it")
		self.assertEqual(len(self._journeys()), 2)


class TestTheToggleIsDeclaredLikeEveryOtherSetting(FrappeTestCase):
	"""It is a Trigger config field, so it travels the ONE declaration the canvas already reads."""

	def test_the_trigger_declares_it(self):
		names = [f["name"] for f in registry.config_fields(registry.TRIGGER)]
		self.assertIn("once_per_subject", names)

	def test_its_type_is_in_the_one_table(self):
		field = next(f for f in registry.config_fields(registry.TRIGGER) if f["name"] == "once_per_subject")
		self.assertIn(field["type"], registry.FIELD_TYPES)

	def test_it_reaches_the_wire_carrying_a_control(self):
		"""A backend field the wire does not carry renders as an empty grey box — no JS error, clean build,
		green suite. The inspector looks up `f.control`, so the payload is what has to be checked."""
		trigger = next(n for n in registry.node_types() if n["type"] == registry.TRIGGER)
		field = next(f for f in trigger["config"] if f["name"] == "once_per_subject")
		self.assertEqual(field["control"], registry.FIELD_TYPES[field["type"]]["control"])
