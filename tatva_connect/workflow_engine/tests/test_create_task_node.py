# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""ONE Create Task node raises ONE task — and a journey may raise two of the same type.

THE BLOCKER THIS SUITE EXISTS FOR. `create_followup_task` keeps one open task per lead per type, which
is exactly right for the lane it was written for (assignment, inbound, a re-fire) and exactly wrong for a
journey: an LSQ sequence routinely calls the patient, waits a fortnight and calls again, and the second
Create Task node found the first node's task still open and silently created NOTHING. No error, no step
log entry saying so — the rep simply never got the second call. `test_both_nodes_really_raise_their_own_task`
is the migration's own test, and on the old code it is red because the second task does not exist.

The fix is a NARROWER key, never an absent one. The node hands its own `custom_workflow_token` to the
helper, so the open-task check is lead + type + THAT node. Hence the two negatives beside it, which are
what stop the fix becoming "the throttle was removed": the SAME node re-firing still yields ONE task
(`test_the_same_node_firing_twice_still_yields_one_task`), and a caller that names no node behaves
byte-for-byte as it always did (`TestTheThrottleKeyIsNarrowerNeverAbsent`).

Everything else here is the quality the node was missing — a subject the author writes, a priority, and a
due date expressed as a delay instead of hand-typed date arithmetic — plus the one test that protects the
PRODUCT: a created task is a SHELL. A task type that demands a location is created happily by a workflow
and still refuses to be completed without one. Creation asks nothing; the rules bite on Done. If a change
ever makes a location-required task refuse to be CREATED, `TestACreatedTaskIsAShell` goes red and says so.

Driven end to end through the real interpreter on real records: a published workflow, a real journey, real
CRM Tasks read back out of the database. Nothing here asserts that a function was called.

Run:
    bench --site wipetest.localhost run-tests --app tatva_connect \\
        --module tatva_connect.workflow_engine.tests.test_create_task_node
"""
import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, add_to_date, get_datetime, now_datetime

from tatva_connect.automation import actions, describe
from tatva_connect.tasks.tasks import create_followup_task
from tatva_connect.taxonomy import labels
from tatva_connect.tests.activity import task_type_fixture as ttf
from tatva_connect.tests.authz.grains import assert_masters_exist
from tatva_connect.workflow_engine import interpreter, refs, registry
from tatva_connect.workflow_engine.tests import fixtures as fx

_TYPE_NAME = "ZZ WF Follow Up Probe"
_VISIT_TYPE_NAME = "ZZ WF Visit Probe"

# The operator switches the completion guards hang off. Armed for the shell suite, restored OFF.
_GUARD_SWITCH = "Task::CRM Task::guards"
_CAPTURE_SWITCH = "Location::Google::capture"

# Deliberately unparseable as a date: it is the "nonsense value" the due-date resolver must degrade on.
_FIRST_NAME = "Zzzz Probe"
_REPORT_DATE = "2027-03-15"
_SUBJECT = "Call about the lab report"
_NOTE = "Patient has not uploaded the documents"
_PRIORITY = "High"


class _CreateTaskBase(FrappeTestCase):
	"""One probe lead and one task type on the graph fixtures' own grain, for the whole class.

	The type is minted on `fx.GRAIN` because Create Task grain-gates every task it raises — a type on any
	other grain would simply never be created and every assertion below would be about nothing. Its schema
	is EMPTY on purpose: a type that declares fields brings `enforce_activity_logged` into the picture, and
	this suite is about what Create Task does, not about what completing an activity demands.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		assert_masters_exist()
		# Registered first so it runs LAST: every cleanup below it writes, and none of them commits.
		cls.addClassCleanup(frappe.db.commit)
		fx.arm_engine(True, cls)
		cls.task_type = ttf.mint_type(
			_TYPE_NAME, [],
			vertical=fx.GRAIN["vertical"], group=fx.GRAIN["group"], program=fx.GRAIN["program"],
		)
		cls.addClassCleanup(ttf.teardown)
		# The lead is born BEFORE any workflow exists, so no Created trigger can start a journey on it.
		cls.lead = fx.make_lead(first_name=_FIRST_NAME, custom_last_report_date=_REPORT_DATE)
		cls.addClassCleanup(
			frappe.delete_doc, "CRM Lead", cls.lead.name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def tearDown(self):
		_clear_tasks(self.lead.name)
		frappe.db.commit()

	# -- driving the real engine -------------------------------------------------------------------

	def _walk(self, *nodes):
		"""Publish a workflow of these nodes, start a journey on the probe lead, walk it. Returns its name."""
		name = f"create-task-probe-{frappe.generate_hash(length=6)}"
		self.addCleanup(fx.purge, name)
		workflow = fx.make_workflow(name, list(nodes))
		run = fx.start_journey(workflow, self.lead.name, "start")
		interpreter.advance(frappe.get_doc(fx.JOURNEY_DT, run.name))
		frappe.db.commit()
		return run.name

	def _task_at(self, journey, node_id):
		"""The task THAT node raised, found by the token it stamps — never "the only task on the lead".

		Asking by token is the whole point: a suite that read back "the task on this lead" could not tell
		one node's task from another's, which is precisely the confusion the blocker was hiding in.
		"""
		return frappe.db.get_value("CRM Task", {"custom_workflow_token": f"{journey}::{node_id}"}, "name")

	def _raise(self, config, task_type=None):
		"""Run ONE Create Task node carrying `config`. Returns the CRM Task document it really raised."""
		journey = self._walk(
			fx.trigger(to="t1"),
			fx.node("t1", "Create Task",
			        config={"task_type": task_type or self.task_type, **config}, edges={"next": "end"}),
			fx.node("end", "Terminal"),
		)
		name = self._task_at(journey, "t1")
		self.assertTrue(name, "the Create Task node raised no task at all")
		return frappe.get_doc("CRM Task", name)

	def _tasks_of(self, task_type=None):
		return frappe.get_all(
			"CRM Task",
			filters={"reference_doctype": "CRM Lead", "reference_docname": self.lead.name,
			         "custom_task_type": task_type or self.task_type},
			pluck="name",
		)


class TestTwoNodesOfOneTypeEitherSideOfAWait(_CreateTaskBase):
	"""THE test the migration depends on. Two Create Task nodes, one task type, one lead, a Wait between."""

	def test_both_nodes_really_raise_their_own_task(self):
		"""Call, wait a fortnight, call again — the shape three transcribed LSQ automations are built on.

		RED on the old code at `second`: the second node asked for one open task of this type on this lead,
		found the first node's, returned it and inserted nothing. The journey walked to Terminal reporting
		`ok`, so the only visible symptom was a rep whose second call never appeared.
		"""
		journey = self._walk(
			fx.trigger(to="t1"),
			fx.node("t1", "Create Task", config={"task_type": self.task_type}, edges={"next": "w1"}),
			fx.node("w1", "Wait", config={"mode": registry.FOR_DURATION, "duration": '{"minutes": 5}'},
			        edges={"next": "t2"}),
			fx.node("t2", "Create Task", config={"task_type": self.task_type}, edges={"next": "end"}),
			fx.node("end", "Terminal"),
		)
		first = self._task_at(journey, "t1")
		self.assertTrue(first, "the first node raised no task, so the blocker cannot be reproduced")
		self.assertIsNone(self._task_at(journey, "t2"),
		                  "the journey is parked at the Wait — the second node has not run yet")

		# The fortnight arrives. The row is the truth (wakeups.py:3), so moving its deadline IS the clock.
		frappe.db.set_value(fx.JOURNEY_DT, journey, "resume_at", add_to_date(now_datetime(), minutes=-1))
		frappe.db.commit()
		interpreter.advance(frappe.get_doc(fx.JOURNEY_DT, journey))
		frappe.db.commit()

		second = self._task_at(journey, "t2")
		self.assertTrue(second, "the second node created NOTHING — the open task of the first node "
		                        "swallowed it, and the patient never gets their follow-up call")
		self.assertNotEqual(first, second, "the two nodes must not share one task")
		self.assertEqual(sorted(self._tasks_of()), sorted([first, second]),
		                 "the lead must carry exactly the two tasks the two nodes raised")
		self.assertEqual(
			frappe.db.get_value(fx.JOURNEY_DT, journey, "status"), "Done",
			"the journey must have walked past the second node to its Terminal",
		)

	def test_the_same_node_firing_twice_still_yields_one_task(self):
		"""The narrower key must not become an absent one: a replayed segment reuses its own open task.

		A retry, a redelivery or a re-drive re-enters the SAME node of the SAME journey, so it mints the
		same token — and finds the task it raised last time. Without this half, the fix would pile a fresh
		task onto a patient every time a worker retried.

		The journey is put back at the node the way a retry arrives at it; nothing else about the row is
		touched, so the token the second pass computes is the one already stamped on the first task.
		"""
		journey = self._walk(
			fx.trigger(to="t1"),
			fx.node("t1", "Create Task", config={"task_type": self.task_type}, edges={"next": "end"}),
			fx.node("end", "Terminal"),
		)
		first = self._task_at(journey, "t1")
		self.assertTrue(first, "the node raised no task, so a replay proves nothing")

		frappe.db.set_value(fx.JOURNEY_DT, journey, {"status": "Running", "current_node": "t1"})
		frappe.db.commit()
		interpreter.advance(frappe.get_doc(fx.JOURNEY_DT, journey))
		frappe.db.commit()

		self.assertEqual(self._tasks_of(), [first],
		                 "a replayed node must reuse its own open task, never raise a second one")

	def test_a_completed_task_does_not_stop_the_node_raising_the_next_one(self):
		"""The throttle is about OPEN tasks, and it still is: the second node runs after the first is Done.

		Worth its own test because it is the ordinary shape of a journey — the rep completes the call, the
		Wait expires, the next call is raised — and a fix that keyed on "any task of this type" rather than
		"any OPEN task of this type" would pass every other test in this class and fail here.
		"""
		journey = self._walk(
			fx.trigger(to="t1"),
			fx.node("t1", "Create Task", config={"task_type": self.task_type}, edges={"next": "w1"}),
			fx.node("w1", "Wait", config={"mode": registry.FOR_DURATION, "duration": '{"minutes": 5}'},
			        edges={"next": "t2"}),
			fx.node("t2", "Create Task", config={"task_type": self.task_type}, edges={"next": "end"}),
			fx.node("end", "Terminal"),
		)
		first = self._task_at(journey, "t1")
		frappe.db.set_value("CRM Task", first, "status", "Done")
		frappe.db.set_value(fx.JOURNEY_DT, journey, "resume_at", add_to_date(now_datetime(), minutes=-1))
		frappe.db.commit()
		interpreter.advance(frappe.get_doc(fx.JOURNEY_DT, journey))
		frappe.db.commit()

		second = self._task_at(journey, "t2")
		self.assertTrue(second, "a closed first task must leave the second node free to raise its own")
		self.assertNotEqual(first, second)


class TestTheThrottleKeyIsNarrowerNeverAbsent(_CreateTaskBase):
	"""The helper's own contract, asked directly — every existing caller must be untouched.

	`create_followup_task` is called by assignment, by the activity transition and by the WhatsApp inbound
	event, none of which name a node. Those callers are the reason the throttle exists at all, and this
	class is what proves the journey fix did not quietly cost them their idempotency.
	"""

	def test_a_caller_that_names_no_node_still_gets_one_open_task_per_lead_per_type(self):
		"""GREEN before and after, deliberately: this is the regression guard, not the new behaviour."""
		first = create_followup_task(lead=self.lead.name, task_type=self.task_type)
		again = create_followup_task(lead=self.lead.name, task_type=self.task_type)

		self.assertEqual(first, again, "a second call naming no node must reuse the open task")
		self.assertEqual(self._tasks_of(), [first], "the lead must carry exactly one task")

	def test_two_different_nodes_of_one_type_each_get_their_own_task(self):
		"""The narrowing, at the seam. RED on the old code: `node_token` was not a parameter at all."""
		first = create_followup_task(lead=self.lead.name, task_type=self.task_type, node_token="RUN-A::t1")
		actions._stamp_workflow_token(first, "RUN-A::t1")
		second = create_followup_task(lead=self.lead.name, task_type=self.task_type, node_token="RUN-A::t2")
		actions._stamp_workflow_token(second, "RUN-A::t2")

		self.assertNotEqual(first, second, "a second node must get its own task, not the first node's")
		self.assertEqual(sorted(self._tasks_of()), sorted([first, second]))

	def test_the_stamp_is_what_makes_the_narrowed_key_idempotent(self):
		"""The ordering the node depends on, pinned: the helper READS the token, `_stamp_workflow_token`
		writes it. Stamp after the insert and a re-fire finds its own task; skip the stamp and the check can
		never match, so the node would raise a fresh task on every pass. One writer, and this is its bond.
		"""
		token = "RUN-B::t1"
		first = create_followup_task(lead=self.lead.name, task_type=self.task_type, node_token=token)
		actions._stamp_workflow_token(first, token)
		again = create_followup_task(lead=self.lead.name, task_type=self.task_type, node_token=token)

		self.assertEqual(first, again, "the same node must reuse its own open task")
		self.assertEqual(self._tasks_of(), [first])

	def test_the_stamp_never_moves_a_token_that_is_already_there(self):
		"""`_stamp_workflow_token` writes only where there is none — the throttle can return an open task
		from an earlier node, and re-badging it would hand that node's task to somebody else's Wait."""
		token = "RUN-C::t1"
		task = create_followup_task(lead=self.lead.name, task_type=self.task_type, node_token=token)
		actions._stamp_workflow_token(task, token)
		actions._stamp_workflow_token(task, "RUN-C::t9")

		self.assertEqual(frappe.db.get_value("CRM Task", task, "custom_workflow_token"), token)


class TestTheSubjectTheAuthorWrites(_CreateTaskBase):
	"""Item 3 — the line the rep actually reads on their list."""

	def test_an_authored_line_is_what_the_rep_reads(self):
		"""RED on the old code: the title was always the task type's name, whatever the author wrote."""
		task = self._raise({"subject_mode": refs.LITERAL, "subject_text": _SUBJECT})

		self.assertEqual(task.title, _SUBJECT)

	def test_a_subject_built_from_the_run_lands_resolved(self):
		"""The expression is evaluated before the task is written — a rep never sees `ctx[...]` on a list."""
		task = self._raise({
			"subject_mode": refs.EXPRESSION,
			"subject_expression": '"Call " + ctx["crm_lead.first_name"]',
		})

		self.assertEqual(task.title, f"Call {_FIRST_NAME}")

	def test_no_subject_still_falls_back_to_the_types_own_name(self):
		"""GREEN before and after: an existing node must read exactly as it did, and the composite
		`vertical::group::program::type_name` key must never reach a screen."""
		task = self._raise({})

		self.assertEqual(task.title, labels.label(self.task_type, labels.TASK_TYPE))
		self.assertEqual(task.title, _TYPE_NAME)
		self.assertNotIn("::", task.title, "the grain-composite task_type key leaked into the subject")

	def test_a_blank_authored_subject_is_the_same_as_no_subject(self):
		"""An author who picks Literal and types nothing gets the type's name, not an empty task list row."""
		task = self._raise({"subject_mode": refs.LITERAL, "subject_text": "   "})

		self.assertEqual(task.title, _TYPE_NAME)


class TestTheNoteTheAuthorWrites(_CreateTaskBase):
	"""The subject's twin — the same two modes, and the note every workflow in flight already carries."""

	def test_a_typed_note_lands_under_the_subject(self):
		task = self._raise({"description_mode": refs.LITERAL, "description": _NOTE})

		self.assertEqual(task.description, _NOTE)

	def test_a_note_built_from_the_run_lands_resolved(self):
		"""What the mode exists for: the rep reads the patient's own words, never `ctx[...]`.

		Read back off the lead rather than compared to the constant it was born with: this site's own
		Active workflows fire on the probe lead too, and a note that resolved perfectly then failed on a
		name somebody else had edited would read as a defect in this node."""
		task = self._raise({
			"description_mode": refs.EXPRESSION,
			"description_expression": '"Replied: " + ctx["crm_lead.first_name"]',
		})

		first_name = frappe.db.get_value("CRM Lead", self.lead.name, "first_name")
		self.assertEqual(task.description, f"Replied: {first_name}")

	def test_a_note_authored_before_the_mode_existed_still_writes(self):
		"""THE COMPATIBILITY TEST. Every note in flight was seeded as `description` with no mode
		(2026-08-09-anaya-all-workflows writes exactly this shape). An unset mode reads the literal box."""
		task = self._raise({"description": _NOTE})

		self.assertEqual(task.description, _NOTE)

	def test_no_note_is_still_no_note(self):
		self.assertFalse(self._raise({}).description)


class TestThePriorityTheAuthorPicks(_CreateTaskBase):
	"""Item 4 — set when given, left alone when not."""

	def _default_priority(self):
		"""What a CRM Task carries when nobody sets a priority — asked of a real record, never typed here."""
		probe = frappe.get_doc({
			"doctype": "CRM Task", "title": "ZZ priority probe", "status": "Backlog",
		}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input
		self.addCleanup(frappe.delete_doc, "CRM Task", probe.name, force=True, ignore_permissions=True)
		return probe.priority

	def test_an_authored_priority_lands_on_the_task(self):
		"""RED on the old code: the node had no priority parameter, so nothing was ever set."""
		task = self._raise({"priority": _PRIORITY})

		self.assertEqual(task.priority, _PRIORITY)

	def test_no_priority_leaves_the_record_its_own_default(self):
		"""The other half — a node that authored nothing must not have decided anything either."""
		default = self._default_priority()
		self.assertNotEqual(default, _PRIORITY,
		                    "premise: the authored priority must differ from the default, or the "
		                    "assertion below passes without meaning anything")

		task = self._raise({})

		self.assertEqual(task.priority, default)


class TestWhenTheTaskFallsDue(_CreateTaskBase):
	"""Item 5 — a delay an author writes, beside the two modes that already worked."""

	def _assert_close(self, actual, expected, why):
		"""Due dates resolved against two different `now`s, so they are compared with a tolerance."""
		self.assertLess(abs((get_datetime(actual) - get_datetime(expected)).total_seconds()), 120, why)

	def test_a_delay_falls_due_that_far_from_now(self):
		"""RED on the old code: `After a delay` was not a mode, so `_due_at` fell through to From Context,
		found no `due_from`, returned None — and the task took its 4-hour default instead of +14 days."""
		expected = add_to_date(now_datetime(), days=14)

		task = self._raise({"due_mode": actions.DUE_AFTER_DELAY, "due_delay": '{"days": 14}'})

		self._assert_close(task.due_date, expected, "a 14-day delay must fall due in 14 days")

	def test_the_delay_lands_where_the_same_delay_written_on_a_wait_lands(self):
		"""ONE delay arithmetic. If this node ever grew its own, "in 14 days" would mean two things in one
		journey — the Wait's instant and the task's due date drifting apart with nothing going red."""
		base = now_datetime()
		expected = actions.wait_resume_at('{"days": 14}', refs.Values(), base)

		task = self._raise({"due_mode": actions.DUE_AFTER_DELAY, "due_delay": '{"days": 14}'})

		self._assert_close(task.due_date, expected, "the node and the Wait must resolve a delay identically")

	def test_a_date_the_run_is_carrying_is_used_as_it_is(self):
		"""From Context, untouched by the new mode."""
		task = self._raise({"due_mode": refs.FROM_CONTEXT, "due_from": "crm_lead.custom_last_report_date"})

		self.assertEqual(get_datetime(task.due_date), get_datetime(_REPORT_DATE))

	def test_an_expression_still_does_its_arithmetic(self):
		"""Expression, untouched by the new mode."""
		task = self._raise({
			"due_mode": refs.EXPRESSION,
			"due_expression": 'add_days(ctx["crm_lead.custom_last_report_date"], 7)',
		})

		self.assertEqual(get_datetime(task.due_date), get_datetime(add_days(_REPORT_DATE, 7)))

	def test_a_value_that_is_not_a_date_leaves_the_default_and_never_drops_the_task(self):
		"""The task matters more than its due date: a value that cannot be a date degrades to the helper's
		own lead time. A journey must not lose a patient's follow-up because an author picked the wrong
		variable, and the rep must not be handed a task due at a moment nobody chose."""
		expected = add_to_date(now_datetime(), hours=4)

		task = self._raise({"due_mode": refs.FROM_CONTEXT, "due_from": "crm_lead.first_name"})

		self.assertTrue(task.name, "a nonsense due date must never cost the lead its task")
		self._assert_close(task.due_date, expected, "it must fall back to the helper's default lead time")


class TestACreatedTaskIsAShell(_CreateTaskBase):
	"""ITEM 6, THE ONE THAT PROTECTS THE PRODUCT.

	A workflow raises an OPEN TO-DO. It fills no form, answers no rule and captures no coordinates, because
	none of that exists until a rep does the work. Every denial — the activity log, the checklist, the
	location — fires on the transition to Done, and this suite pins BOTH halves in one test so nobody can
	"fix" the creation side by making a location-required type refuse to be raised at all.

	The tracked grain and the two switches are set up FOR REAL rather than patched: the guard reads
	operator config, and a suite that stubbed it out would prove the refusal fires against a stub.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.visit_type = ttf.mint_type(
			_VISIT_TYPE_NAME, [],
			vertical=fx.GRAIN["vertical"], group=fx.GRAIN["group"], program=fx.GRAIN["program"],
			extra={"visit_mode": "In-Person"},
		)
		_arm_location(cls)

	def test_a_location_required_task_is_created_and_still_cannot_be_completed_without_one(self):
		"""Both halves, deliberately in one test — they are one rule seen from two ends.

		RED-on-old-code is not the point here; this is the lock that keeps items 2-5 from breaking it. Break
		the creation half (make the shell run the form layer) and the first assertion goes red; break the
		completion half (let a workflow-raised task skip the guard) and the last one does.
		"""
		from tatva_connect.location.api import location_required

		task_name = self._raise({}, task_type=self.visit_type).name

		row = frappe.db.get_value(
			"CRM Task", task_name,
			["status", "custom_location_latitude", "custom_location_longitude"], as_dict=True,
		)
		self.assertEqual(row.status, "Todo", "a raised task is an open to-do, never a logged activity")
		self.assertFalse(row.custom_location_latitude, "creation must capture no coordinates")
		self.assertFalse(row.custom_location_longitude, "creation must capture no coordinates")

		# Premise: without a live guard the refusal below would be proving nothing at all.
		self.assertIsNotNone(
			location_required(self.visit_type, self.lead.name, {}),
			"the location guard is not live for this type on this lead — the refusal cannot be tested",
		)

		doc = frappe.get_doc("CRM Task", task_name)
		doc.status = "Done"
		with self.assertRaises(frappe.ValidationError) as refused:
			doc.save(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input

		self.assertIn("location", str(refused.exception).lower(),
		              "the refusal must be the location one, not some other validation")
		self.assertEqual(frappe.db.get_value("CRM Task", task_name, "status"), "Todo",
		                 "the refused save must have left the task open")

	def test_the_journey_walked_on_after_raising_it(self):
		"""A location-required type must not park or fail the journey either — the node ran, and that is all
		that happened at it."""
		journey = self._walk(
			fx.trigger(to="t1"),
			fx.node("t1", "Create Task", config={"task_type": self.visit_type}, edges={"next": "end"}),
			fx.node("end", "Terminal"),
		)

		self.assertEqual(frappe.db.get_value(fx.JOURNEY_DT, journey, "status"), "Done")
		self.assertTrue(self._task_at(journey, "t1"))


class TestTheNodeOffersWhatAnAuthorNeeds(FrappeTestCase):
	"""The declaration itself. Cheap, and red on the old code for every field items 3-5 added."""

	def _params(self):
		return {p["name"]: p for p in actions.params_of("Create Task")}

	def test_the_author_can_write_a_subject(self):
		params = self._params()

		self.assertIn("subject_mode", params, "there is no way to write a subject")
		self.assertEqual(params["subject_mode"]["options"], [refs.LITERAL, refs.EXPRESSION])
		self.assertEqual(params["subject_expression"]["reads"], "expression",
		                 "a subject expression the gates cannot read is one publish never checks")

	def test_the_author_can_write_a_note_the_same_way_they_write_a_subject(self):
		"""One shape for "write some text", and a `Literal` DEFAULT the subject does not need: a note
		authored before the mode existed carries no mode, and the default is what keeps its box on screen."""
		params = self._params()

		self.assertEqual(params["description_mode"]["options"], [refs.LITERAL, refs.EXPRESSION])
		self.assertEqual(params["description_mode"]["default"], refs.LITERAL)
		self.assertEqual(params["description_expression"]["reads"], "expression",
		                 "a note expression the gates cannot read is one publish never checks")
		on_panel = [f["name"] for f in registry.applied_fields("Create Task", {"description": _NOTE})]
		self.assertIn("description", on_panel,
		              "a note with no mode must stay on the panel, or the save that drops it is silent")

	def test_the_author_can_set_a_priority_from_the_records_own_vocabulary(self):
		"""Not a typed triple: add a priority to `CRM Task` and the node offers it on the next read."""
		field = frappe.get_meta("CRM Task").get_field("priority")

		self.assertEqual(
			self._params()["priority"]["options"],
			describe._value_options("Select", field.options if field else ""),
		)

	def test_the_author_can_express_a_due_date_as_a_delay(self):
		"""And with the WAIT's own control, so "in 14 days" is authored the same way wherever it is written."""
		params = self._params()

		self.assertIn(actions.DUE_AFTER_DELAY, params["due_mode"]["options"])
		self.assertEqual(params["due_delay"]["type"], "Duration")
		self.assertEqual(params["due_delay"]["depends_on_value"], {"due_mode": [actions.DUE_AFTER_DELAY]})

	def test_the_author_can_pick_an_assignee(self):
		"""The same two controls as Assign to User — one vocabulary for the same question."""
		params = self._params()

		self.assertIn("assignee_mode", params, "there is no way to pick an assignee")
		self.assertEqual(params["assignee_mode"]["options"], ["User", "From Variable"])
		# User field mirrors Assign to User's — link, scope, depends
		self.assertEqual(params["assign_to_user"]["type"], "Link")
		self.assertEqual(params["assign_to_user"]["link"], "User")
		self.assertEqual(params["assign_to_user"]["scope"], "entitled_users")
		self.assertEqual(params["assign_to_user"]["depends_on_value"], {"assignee_mode": ["User"]})
		# Variable field mirrors Assign to User's too
		self.assertEqual(params["assignee_variable"]["type"], "Variable")
		self.assertEqual(params["assignee_variable"]["depends_on_value"], {"assignee_mode": ["From Variable"]})


class TestTheTaskIsAssignedAsTheAuthorOrdained(_CreateTaskBase):
	"""The assignee lands where the author chose — or auto-resolves when no choice is made."""

	def test_no_mode_chosen_lands_with_the_lead_owner(self):
		"""Default (auto): on a Lead-triggered rule the trigger doc is the lead, which has no
		`assigned_to`, so the fallback to `lead_owner` fires. The task lands with the lead's owner."""
		task = self._raise({})

		self.assertEqual(task.assigned_to, frappe.db.get_value("CRM Lead", self.lead.name, "lead_owner"))

	def test_user_mode_assigns_to_the_named_person(self):
		"""The author names one person — the task lands with that person, nobody else."""
		task = self._raise({
			"assignee_mode": "User",
			"assign_to_user": "Administrator",
		})

		self.assertEqual(task.assigned_to, "Administrator")

	def test_from_variable_assigns_from_context(self):
		"""From Variable reads a user id out of the run's state — the same mechanism Assign to User
		uses, and the same edge case: an unresolvable variable yields an unassigned task."""
		journey = self._walk(
			fx.trigger(to="sv"),
			fx.node("sv", "Set Variables", config={"assignments": [
				{"variable": "who", "expression": '"Administrator"'},
			]}, edges={"next": "t1"}),
			fx.node("t1", "Create Task", config={
				"task_type": self.task_type,
				"assignee_mode": "From Variable",
				"assignee_variable": "sv.who",
			}, edges={"next": "end"}),
			fx.node("end", "Terminal"),
		)
		name = self._task_at(journey, "t1")

		self.assertTrue(name)
		self.assertEqual(frappe.db.get_value("CRM Task", name, "assigned_to"), "Administrator")


def _arm_location(cls):
	"""Make the probe grain really location-tracked, and disarm it however the class ends.

	Every restore is REGISTERED BEFORE the write it undoes, so an abort part-way through still leaves the
	bench clean — `fixtures.arm_engine`'s reasoning, and for its reason: a switch left ON is what makes a
	later "no switches were left on" claim unfalsifiable. The restore goes to OFF, never to what it was.
	"""
	settings = frappe.get_doc("CRM Maps Settings")
	before = {row.name for row in settings.location_tracked_grains}
	cls.addClassCleanup(frappe.db.commit)
	cls.addClassCleanup(_untrack_grains, before)
	cls.addClassCleanup(frappe.db.set_value, "CRM Tatva Automation", _CAPTURE_SWITCH, "enabled", 0)
	cls.addClassCleanup(frappe.db.set_value, "CRM Tatva Automation", _GUARD_SWITCH, "enabled", 0)
	frappe.db.set_value("CRM Tatva Automation", _GUARD_SWITCH, "enabled", 1)
	frappe.db.set_value("CRM Tatva Automation", _CAPTURE_SWITCH, "enabled", 1)
	settings.append("location_tracked_grains", {
		"vertical": fx.GRAIN["vertical"], "group": fx.GRAIN["group"], "program": fx.GRAIN["program"],
		"radius_m": 100,
	})
	settings.save(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input
	frappe.db.commit()


def _untrack_grains(before):
	"""Drop only the row this suite appended — an operator's own tracked grain is not ours to delete."""
	settings = frappe.get_doc("CRM Maps Settings")
	settings.set("location_tracked_grains",
	             [row for row in settings.location_tracked_grains if row.name in before])
	settings.save(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input


class TestRunSeedPreservesTriggerContext(FrappeTestCase):
	"""`_run_seed` strips the subject bucket to `__before` pairs but keeps trigger-doc buckets in full.

	The subject (crm_lead) has a live record loader — its current values are always read fresh. Every
	other bucket (crm_task, file, whatsapp_message) has no loader, so its snapshot at trigger time IS
	the author's reference point. Stripping it silently defaults due dates and subjects on the durable
	path — the gap this class locks.
	"""

	def test_subject_bucket_is_stripped_to_before_pairs(self):
		from tatva_connect.workflow_engine import refs, triggers

		ctx = refs.Values(buckets={"crm_lead": {"status": "Qualified", "status__before": "New"}})
		result = triggers._run_seed(ctx)

		self.assertEqual(result, {"crm_lead": {"status__before": "New"}},
		                 "the subject bucket must keep only __before pairs — current values come from the live loader")

	def test_trigger_task_bucket_is_kept_in_full(self):
		from tatva_connect.workflow_engine import refs, triggers

		ctx = refs.Values(buckets={"crm_task": {"due_date": "2026-08-20", "assigned_to": "rep@x",
		                                         "status": "Done", "status__before": "Todo"}})
		result = triggers._run_seed(ctx)

		self.assertEqual(
			result["crm_task"]["due_date"], "2026-08-20",
			"the trigger task's due_date was silently discarded — on the durable path it defaulted to +4h",
		)
		self.assertEqual(result["crm_task"]["assigned_to"], "rep@x")
		self.assertEqual(result["crm_task"]["status"], "Done")
		self.assertEqual(result["crm_task"]["status__before"], "Todo",
		                 "__before pairs must still be there — a Route predicate reads them")

	def test_file_bucket_is_kept_in_full(self):
		from tatva_connect.workflow_engine import refs, triggers

		ctx = refs.Values(buckets={"file": {"file_url": "/private/files/doc.pdf", "file_size": 1024}})
		result = triggers._run_seed(ctx)

		self.assertEqual(result, {"file": {"file_url": "/private/files/doc.pdf", "file_size": 1024}})

	def test_whatsapp_message_bucket_is_kept_in_full(self):
		from tatva_connect.workflow_engine import refs, triggers

		ctx = refs.Values(buckets={"whatsapp_message": {"message": "Hello", "message_id": "wa_123"}})
		result = triggers._run_seed(ctx)

		self.assertEqual(result, {"whatsapp_message": {"message": "Hello", "message_id": "wa_123"}})


def _clear_tasks(lead):
	for name in frappe.get_all(
		"CRM Task", filters={"reference_doctype": "CRM Lead", "reference_docname": lead}, pluck="name"
	):
		frappe.delete_doc("CRM Task", name, force=True, ignore_permissions=True)
