# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""W7.2 / W14 — the Trigger's schedule: what it declares, when it next runs, what publish refuses, what an author sees.

A cohort is a journey FACTORY, not a second engine: the workflow drain walks each selected lead down the identical
graph. This suite holds the half that DECLARES a cohort — the Trigger's schedule mode, the columns the drain
queries, the next-run arithmetic, the publish rules, and the readout an author sees before arming.

THE SINGULAR RULE, AND THE ONE PLACE IT COULD BE BROKEN. `W1-contract.md:212` rejects a node changing
shape because of ANOTHER node's mode — Send WhatsApp's `contact_number` appearing "only on a scheduled
trigger". A node gating its OWN fields on its OWN mode is the sanctioned pattern and Wait has shipped it
since W1 (`registry.py:111-122`). So the Trigger gates its own `event`/`schedule`, and
`test_no_other_node_learns_the_trigger_has_modes` holds that line shut.
"""
from datetime import datetime

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.workflow_engine import cohort, registry
from tatva_connect.workflow_engine.tests import fixtures as fx

# A Thursday, 10:00 — past the 09:30 run of the same day, so "today" and "next week" are both exercised.
_NOW = datetime(2026, 9, 17, 10, 0)

_WORKFLOW = "cohort-trigger-probe"
_SCHEDULED = "cohort-trigger-scheduled"


def _trigger_fields():
	return {f["name"]: f for f in registry.declaration(registry.TRIGGER)["config"]}


class TestTheTriggerDeclaresScheduleMode(FrappeTestCase):
	"""One node, one shape. The mode gates its OWN fields and nothing else in the graph hears about it."""

	def test_the_trigger_declares_both_modes(self):
		mode = _trigger_fields()["mode"]
		self.assertEqual(mode["type"], "Select")
		self.assertEqual(sorted(mode["options"]), sorted([registry.MODE_RECORD, registry.MODE_SCHEDULE]))

	def test_the_event_belongs_to_record_mode_and_the_schedule_to_schedule_mode(self):
		fields = _trigger_fields()
		self.assertEqual(fields["event"]["depends_on_value"], {"mode": [registry.MODE_RECORD]})
		self.assertEqual(fields["schedule"]["depends_on_value"], {"mode": [registry.MODE_SCHEDULE]})

	def test_the_subject_grain_and_criteria_are_declared_in_BOTH_modes(self):
		"""The criteria field is the SAME predicate in both modes — "only when" on a record event is "who
		is in the cohort" on a schedule. A second criteria language is the defect this avoids."""
		for name in ("subject_doctype", "vertical", "group", "program", "predicate"):
			self.assertNotIn("depends_on_value", _trigger_fields()[name],
			                 f"{name} must be declared in every mode, exactly as `contact_number` is")

	def test_a_gated_off_field_is_not_required(self):
		"""A schedule Trigger must not be asked for an Event. `registry._applies` already answers this —
		the test exists because the whole mode split rests on it."""
		schedule_cfg = {"mode": registry.MODE_SCHEDULE}
		applied = {f["name"] for f in registry.applied_fields(registry.TRIGGER, schedule_cfg)}
		self.assertIn("schedule", applied)
		self.assertNotIn("event", applied)

	def test_no_other_node_learns_the_trigger_has_modes(self):
		"""THE LOCK. `W1-contract.md:212` rejected exactly this: a send node whose shape changes because
		of the Trigger. Only the Trigger may gate on `mode`."""
		for node_type in registry.node_types():
			if node_type["type"] == registry.TRIGGER:
				continue
			for field in node_type.get("config") or []:
				gate = field.get("depends_on_value") or {}
				self.assertNotIn(
					"mode", {k for k in gate} if node_type["type"] != "Wait" else set(),
					f"{node_type['type']}.{field['name']} gates on a `mode` it does not own",
				)


class TestTheHeaderCarriesWhatTheDrainWillQuery(FrappeTestCase):
	"""`Which workflows are due?` must be ONE INDEXED QUERY, so the answer is materialised onto the header
	off the Trigger — the same brain that already materialises `trigger_doctype`/`trigger_event`."""

	@classmethod
	def tearDownClass(cls):
		fx.purge(_WORKFLOW, _SCHEDULED)
		super().tearDownClass()

	def setUp(self):
		fx.purge(_WORKFLOW, _SCHEDULED)

	def _scheduled(self, schedule="Monthly", at="09:00:00"):
		trigger = fx.node("start", "Trigger", config={
			"mode": registry.MODE_SCHEDULE, "subject_doctype": "CRM Lead",
			"schedule": schedule, "schedule_time": at,
			"vertical": fx.GRAIN["vertical"], "group": fx.GRAIN["group"], "program": fx.GRAIN["program"],
		}, edges={"next": "end"})
		return fx.make_workflow(_SCHEDULED, [trigger, fx.node("end", "Terminal")], lifecycle_state="Draft")

	def test_a_record_workflow_carries_no_next_run(self):
		wf = fx.make_workflow(_WORKFLOW, [fx.trigger(to="end"), fx.node("end", "Terminal")],
		                      lifecycle_state="Draft")
		row = frappe.db.get_value("CRM Workflow", wf.name, ["trigger_mode", "trigger_next_run_at"], as_dict=True)
		self.assertEqual(row.trigger_mode, registry.MODE_RECORD)
		self.assertIsNone(row.trigger_next_run_at, "a record-event workflow is never due; it is woken by a save")

	def test_a_scheduled_workflow_carries_its_next_run(self):
		wf = self._scheduled()
		row = frappe.db.get_value("CRM Workflow", wf.name, ["trigger_mode", "trigger_next_run_at"], as_dict=True)
		self.assertEqual(row.trigger_mode, registry.MODE_SCHEDULE)
		self.assertIsNotNone(row.trigger_next_run_at, "the drain finds a due workflow by this column alone")

	def test_the_next_run_tracks_the_schedule_the_node_carries(self):
		"""Re-saving after an edit re-computes the column from the node, rather than leaving the value the
		previous schedule produced.

		Asserted against `next_run_at` itself and NOT as "it differs from before": no two frequencies are
		collision-proof at every anchor — on the last day of a month, Monthly and Daily are both tomorrow —
		so a difference check is green on 30 days and red on the 31st.
		"""
		wf = self._scheduled(schedule="Monthly")
		node = frappe.get_doc("CRM Workflow Node", frappe.get_all(
			"CRM Workflow Node", filters={"workflow": wf.name, "node_id": "start"}, pluck="name")[0])
		config = frappe.parse_json(node.config_json)
		config["schedule"] = "Daily"
		node.config_json = frappe.as_json(config)
		node.save(ignore_permissions=True)
		frappe.get_doc("CRM Workflow", wf.name).save(ignore_permissions=True)
		self.assertEqual(
			frappe.db.get_value("CRM Workflow", wf.name, "trigger_next_run_at"),
			cohort.next_run_at(config),
			"the stored due time no longer agrees with the schedule the node carries",
		)

	def test_the_due_query_is_one_indexed_read(self):
		"""The index is the whole reason these columns exist. Declared in `schema_setup._STEPS` as well as
		its patch, because `install-app` BASELINES a patch without running it and a fresh site would
		otherwise never get the index with nothing going red."""
		self.assertTrue(frappe.db.has_index("tabCRM Workflow", cohort.DUE_INDEX))


class TestPublishRefusesAnIncompleteSchedule(FrappeTestCase):
	"""A schedule that cannot be read is a cohort that never fires, silently — the exact class of defect
	the publish gate exists to catch, and unfixable once a lead is waiting on it."""

	def _problems(self, config):
		nodes = [
			{"node_id": "start", "node_type": "Trigger", "config": config,
			 "edges": [{"from_output": "next", "to_node": "end"}]},
			{"node_id": "end", "node_type": "Terminal", "config": {}, "edges": []},
		]
		from tatva_connect.workflow_engine import graph

		return [p for p in graph.problems(nodes, "start") if p.get("code", "").startswith("trigger.schedule")]

	def test_a_once_date_already_past_is_refused(self):
		problems = self._problems({"mode": registry.MODE_SCHEDULE, "subject_doctype": "CRM Lead",
		                           "schedule": registry.ONCE, "schedule_date": "2000-01-01"})
		self.assertEqual([p["code"] for p in problems], ["trigger.schedule.past"])

	def test_an_end_before_the_next_run_is_refused(self):
		problems = self._problems({"mode": registry.MODE_SCHEDULE, "subject_doctype": "CRM Lead",
		                           "schedule": registry.DAILY, "schedule_start": "2999-01-02", "schedule_end": "2999-01-01"})
		self.assertEqual([p["code"] for p in problems], ["trigger.schedule.past"])

	def test_each_repeat_asks_for_the_pick_it_needs(self):
		for repeat, needed in ((registry.ONCE, "schedule_date"), (registry.WEEKLY, "schedule_weekdays"),
		                       (registry.MONTHLY, "schedule_month_day")):
			with self.subTest(repeat=repeat):
				problems = registry.validate_node(registry.TRIGGER, {
					"mode": registry.MODE_SCHEDULE, "subject_doctype": "CRM Lead", "schedule": repeat,
				}, ["next"], mode=registry.PUBLISH)
				self.assertIn(needed, {p["field"] for p in problems if p.get("code") == "field.required"})

	def test_a_schedule_trigger_without_a_schedule_is_refused(self):
		problems = self._problems({"mode": registry.MODE_SCHEDULE, "subject_doctype": "CRM Lead"})
		self.assertTrue(problems, "a schedule mode with no schedule publishes a workflow that never fires")
		self.assertEqual(problems[0]["field"], "schedule")

	def test_a_record_trigger_is_not_asked_for_a_schedule(self):
		self.assertFalse(self._problems({
			"mode": registry.MODE_RECORD, "subject_doctype": "CRM Lead", "event": "Created",
		}))


class TestThePreviewCountsThroughTheExistingMatcher(FrappeTestCase):
	"""'This will start 3,140 journeys', shown BEFORE arming. It counts with the same grain matcher and the
	same predicate evaluator the record-event lane uses — never a SQL translation of the criteria, which
	would be a second criteria brain that could disagree with the one that actually runs."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.leads = [fx.make_lead() for _ in range(3)]
		frappe.db.set_value("CRM Lead", cls.leads[0].name, "status", "Qualified")
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		for lead in cls.leads:
			frappe.delete_doc("CRM Lead", lead.name, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	def _config(self, predicate=None):
		config = {
			"mode": registry.MODE_SCHEDULE, "subject_doctype": "CRM Lead",
			"schedule": "Daily", "schedule_time": "09:00:00",
			"vertical": fx.GRAIN["vertical"], "group": fx.GRAIN["group"], "program": fx.GRAIN["program"],
		}
		if predicate:
			config["predicate"] = predicate
		return config

	def test_the_grain_alone_counts_every_lead_on_it(self):
		result = cohort.preview(frappe.as_json(self._config()))
		self.assertGreaterEqual(result["count"], 3)
		self.assertFalse(result["capped"])

	def test_a_predicate_narrows_the_count(self):
		wide = cohort.preview(frappe.as_json(self._config()))["count"]
		narrow = cohort.preview(frappe.as_json(self._config(
			predicate={"type": "rule", "field": "crm_lead.status", "operator": "is", "value": "Qualified"},
		)))["count"]
		self.assertLess(narrow, wide, "the predicate must actually narrow the cohort")
		self.assertGreaterEqual(narrow, 1)

	def test_the_count_is_capped_rather_than_unbounded(self):
		"""A preview that walks a million leads is an outage dressed as a helpful number."""
		result = cohort.preview(frappe.as_json(self._config()), cap=2)
		self.assertEqual(result["count"], 2)
		self.assertTrue(result["capped"], "past the cap the answer is 'more than N', never a wrong number")

	def test_a_record_event_trigger_has_no_cohort_to_preview(self):
		result = cohort.preview(frappe.as_json({
			"mode": registry.MODE_RECORD, "subject_doctype": "CRM Lead", "event": "Created",
		}))
		self.assertIsNone(result["count"], "a record-event workflow starts one journey per save, not a cohort")


class TestTheNextRunIsTheScheduleAsConfigured(FrappeTestCase):
	"""`next_run_at` is the one reader of every schedule field; the drain, the header and the readout all ask it."""

	def _next(self, **fields):
		return cohort.next_run_at({"mode": registry.MODE_SCHEDULE, "schedule_time": "09:30", **fields}, after=_NOW)

	def test_each_repeat_lands_where_the_author_set_it(self):
		for fields, expected in (
			({"schedule": registry.DAILY}, datetime(2026, 9, 18, 9, 30)),
			({"schedule": registry.WEEKLY, "schedule_weekdays": ["Tuesday", "Thursday"]}, datetime(2026, 9, 22, 9, 30)),
			({"schedule": registry.MONTHLY, "schedule_month_day": "15"}, datetime(2026, 10, 15, 9, 30)),
			({"schedule": registry.MONTHLY, "schedule_month_day": registry.LAST_DAY}, datetime(2026, 9, 30, 9, 30)),
			({"schedule": registry.ONCE, "schedule_date": "2026-10-05"}, datetime(2026, 10, 5, 9, 30)),
		):
			with self.subTest(fields=fields):
				self.assertEqual(self._next(**fields), expected)

	def test_a_start_and_an_end_bound_the_runs(self):
		self.assertEqual(self._next(schedule=registry.DAILY, schedule_start="2026-10-01"), datetime(2026, 10, 1, 9, 30))
		self.assertEqual(self._next(schedule=registry.DAILY, schedule_end="2026-09-18"), datetime(2026, 9, 18, 9, 30))
		self.assertIsNone(self._next(schedule=registry.DAILY, schedule_end="2026-09-17"), "a run past the end was offered")

	def test_a_once_run_already_past_has_no_next_run(self):
		self.assertIsNone(self._next(schedule=registry.ONCE, schedule_date="2026-09-17"))

	def test_a_schedule_saved_before_the_picks_existed_keeps_its_meaning(self):
		"""Weekly meant Sunday and Monthly meant the 1st; a config carrying neither pick still does."""
		self.assertEqual(self._next(schedule=registry.WEEKLY), datetime(2026, 9, 20, 9, 30))
		self.assertEqual(self._next(schedule=registry.MONTHLY), datetime(2026, 10, 1, 9, 30))


class TestTheReadoutAnswersTheAuthor(FrappeTestCase):
	def test_it_names_the_next_run_and_the_cohort(self):
		rows = cohort.schedule_readout(frappe.as_json({
			"mode": registry.MODE_SCHEDULE, "subject_doctype": "CRM Lead", "schedule": registry.DAILY,
			"schedule_time": "09:00", "vertical": fx.GRAIN["vertical"], "group": fx.GRAIN["group"],
			"program": fx.GRAIN["program"],
		}))
		self.assertEqual([row["label"] for row in rows], ["Next run", "Matches now"])
		self.assertTrue(all(row["value"] for row in rows))

	def test_a_schedule_with_no_run_ahead_says_so(self):
		rows = cohort.schedule_readout(frappe.as_json({
			"mode": registry.MODE_SCHEDULE, "subject_doctype": "CRM Lead", "schedule": registry.ONCE,
			"schedule_date": "2000-01-01",
		}))
		self.assertEqual(rows[0]["value"], "No more runs")

	def test_the_readout_reaches_the_wire_on_the_trigger(self):
		trigger = next(t for t in registry.node_types() if t["type"] == registry.TRIGGER)
		self.assertEqual(trigger["readout"]["method"], "tatva_connect.workflow_engine.cohort.schedule_readout")
		self.assertEqual(trigger["readout"]["depends_on_value"], {"mode": [registry.MODE_SCHEDULE]})
