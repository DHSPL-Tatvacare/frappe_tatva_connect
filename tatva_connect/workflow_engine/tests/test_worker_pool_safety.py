# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""THE ENGINE IS SAFE UNDER MORE THAN ONE WORKER, AND THAT IS A PROPERTY THIS FILE HOLDS, NOT A CLAIM.

One worker on the `workflow` lane means one journey at a time, so every race in the engine is hidden by the
queue rather than absent from the code. `bench worker-pool --queue workflow --num-workers N` is the
throughput lever we intend to pull the day cohort volume needs it — and pulling it removes that accidental
serialisation in one command, with nothing anywhere going red. What is being locked here is the difference
between "N workers is a config change" and "N workers double-messages patients".

Three guards, and only three, are what make the second worker safe:

  1. `CRM Workflow Journey.active_key` is UNIQUE. Two workers that both decide a lead should enter a
     workflow both insert; the database refuses the second, and `triggers._start_one` reads that refusal as
     "already running" rather than as an error. Without it, two live journeys walk the same lead, each
     raising its own tasks and sending its own messages.
  2. Every row a worker acts on is CLAIMED under `for_update=True` before it is re-checked and written.
     That is the house idiom (no raw SQL, no hand-rolled `UPDATE ... WHERE`): take the row lock, re-check
     the state inside the lock, then act. Delete a claim and two workers both read `Parked`, both advance,
     and the journey takes its next step twice.
  3. The start enqueue is DEDUPLICATED (`job_id` + `deduplicate=True`) and fires only
     `enqueue_after_commit=True`. Several saves inside one request must queue one start, and a start must
     never run inside the saving user's transaction.

WHAT EACH TEST READS, AND HOW HARD.

  * The unique key is asserted TWICE, because the two checks are not equally strong. The DocType's `unique`
    flag is the DECLARATION only — it says what model sync intends, not what the table carries. The
    behavioural test is the real one: it inserts a duplicate and requires the DATABASE to refuse it, which
    proves the constraint without naming the index (frappe picks that name, and guessing it here would make
    this lock go red on a rename rather than on a regression).
  * The claims are read off the AST of each module's source, never counted in the text: `for_update=True`
    appears in these modules' own docstrings, so a string count stays green with every real claim deleted.
  * The enqueue is asserted at RUNTIME on the kwargs `_enqueue_start` really hands `frappe.enqueue` — what
    it passes, not that it was called. `test_workflow_lane_isolation` already holds `enqueue_after_commit`
    structurally across the whole engine; this holds the start's own dedup identity, which no AST walk can
    see because the identity is in the interpolated value.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.workflow_engine.tests.test_worker_pool_safety
"""
import ast
import inspect
import unittest
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.tests.authz.grains import assert_masters_exist
from tatva_connect.workflow_engine import drain, interpreter, signals, triggers, versions, wakeups
from tatva_connect.workflow_engine.tests import fixtures as fx

JOURNEY_DT = fx.JOURNEY_DT
_WF = "ZZ Worker Pool Safety"

# The claim sites, by module and count. A worker acts on a row only after taking its lock, so removing one
# of these is how the engine silently stops being pool-safe.
_CLAIMS = ((interpreter, 2), (signals, 1), (wakeups, 1), (drain, 1))


def _claims(module):
	"""Every `frappe.db.get_value(..., for_update=True)` in a module, as line numbers.

	Walked with AST, never grepped: each of these modules explains its own claim in a docstring, so a text
	count would be satisfied by the prose describing the guard after the guard itself had gone.
	"""
	found = []
	for node in ast.walk(ast.parse(inspect.getsource(module))):
		if not isinstance(node, ast.Call) or getattr(node.func, "attr", None) != "get_value":
			continue
		owner = getattr(node.func, "value", None)
		if getattr(owner, "attr", None) != "db" or getattr(getattr(owner, "value", None), "id", None) != "frappe":
			continue
		for keyword in node.keywords:
			if keyword.arg == "for_update" and isinstance(keyword.value, ast.Constant) and keyword.value.value is True:
				found.append(node.lineno)
	return sorted(found)


class TestTheUniqueKeyIsRealInTheDatabase(FrappeTestCase):
	"""GUARD 1. Two workers racing to start the same lead: the database picks the winner."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		assert_masters_exist()
		fx.purge(_WF)
		cls.addClassCleanup(fx.purge, _WF)
		cls.workflow = fx.make_workflow(_WF, [fx.trigger(to="n1"), fx.node("n1", "Terminal")])
		frappe.db.commit()

	def setUp(self):
		self.lead = fx.make_lead()

	def _insert_journey(self, active_key):
		"""A live journey, inserted the way `_start_one` inserts one — the key is the whole point."""
		return frappe.get_doc({
			"doctype": JOURNEY_DT,
			"workflow": self.workflow.name,
			"workflow_version": versions.current_name(self.workflow.name),
			"subject_doctype": "CRM Lead",
			"subject_name": self.lead.name,
			"current_node": "n1",
			"status": "Running",
			"active_key": active_key,
		}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input

	def test_the_declaration_says_the_key_is_unique(self):
		"""The WEAKER of the two checks, and it is here for the message it gives: a field that loses its
		`unique` tick is a deliberate edit someone must justify, and this names it at the moment it happens.
		It proves intent only — the test below proves the table."""
		field = frappe.get_meta(JOURNEY_DT).get_field("active_key")
		self.assertIsNotNone(field, "active_key is gone from the journey — the double-start guard has no key")
		self.assertTrue(field.unique, "active_key no longer declares unique: model sync will drop the constraint")

	def test_the_database_itself_refuses_a_second_live_journey_for_the_same_lead(self):
		"""THE red. The column was declared unique for months while the table carried no constraint and
		nothing wrote the key, so every matching save started another journey on the same lead."""
		key = f"{self.workflow.name}::{self.lead.name}"
		self._insert_journey(key)
		with self.assertRaises((frappe.UniqueValidationError, frappe.DuplicateEntryError)):
			self._insert_journey(key)

	def test_two_different_leads_both_get_their_journey(self):
		"""The control. Without it, a table that rejected EVERY second insert would pass the test above."""
		self._insert_journey(f"{self.workflow.name}::{self.lead.name}")
		other = fx.make_lead()
		self.assertTrue(self._insert_journey(f"{self.workflow.name}::{other.name}").name)


class TestEveryRowAWorkerActsOnIsClaimedFirst(unittest.TestCase):
	"""GUARD 2. Read the state under the row lock, or two workers read the same state and both act."""

	def test_each_module_still_claims_its_rows(self):
		for module, expected in _CLAIMS:
			with self.subTest(module=module.__name__):
				claims = _claims(module)
				self.assertGreaterEqual(
					len(claims), expected,
					f"{module.__name__} claims {len(claims)} rows for_update, expected at least {expected} — "
					"a worker now acts on a row it never locked",
				)

	def test_the_engine_claims_every_row_the_plan_counted(self):
		"""A walk that found nothing would pass the test above by testing nothing."""
		total = sum(len(_claims(module)) for module, _ in _CLAIMS)
		self.assertGreaterEqual(total, sum(expected for _, expected in _CLAIMS))


class TestTheStartEnqueueCannotFireTwice(unittest.TestCase):
	"""GUARD 3. One start per (workflow, lead) however many times the record is saved, and never before the
	user's own write has committed."""

	def _start_kwargs(self, workflow="ZZ Pool WF A", lead="ZZ-POOL-LEAD-A", version="ZZ Pool V1", seed=None):
		"""What `_enqueue_start` really hands the queue. Nothing runs: the enqueue is the whole subject."""
		with patch.object(frappe, "enqueue") as enqueue:
			triggers._enqueue_start(workflow, version, lead, seed or {})
		self.assertEqual(enqueue.call_count, 1, "the start no longer goes through frappe.enqueue")
		return enqueue.call_args.kwargs

	def test_the_start_job_is_deduplicated_and_identified(self):
		kwargs = self._start_kwargs()
		self.assertIs(kwargs.get("deduplicate"), True, "dedup is off: a burst of saves queues a burst of starts")
		self.assertTrue(kwargs.get("job_id"), "no job_id, so deduplicate has nothing to deduplicate on")

	def test_the_job_id_is_the_workflow_and_the_lead(self):
		"""Identity, not decoration. The id is what two racing saves collide on."""
		kwargs = self._start_kwargs(workflow="ZZ Pool WF A", lead="ZZ-POOL-LEAD-A")
		self.assertIn("ZZ Pool WF A", kwargs["job_id"])
		self.assertIn("ZZ-POOL-LEAD-A", kwargs["job_id"])

	def test_two_saves_of_one_lead_queue_the_same_job(self):
		"""The version, the seed and the trigger record all differ between two saves in one request. None of
		them may enter the id, or every save queues a start of its own."""
		first = self._start_kwargs(version="ZZ Pool V1", seed={"status": "New"})
		second = self._start_kwargs(version="ZZ Pool V2", seed={"status": "Open"})
		self.assertEqual(first["job_id"], second["job_id"])

	def test_two_leads_never_share_a_job(self):
		"""The control: an id that ignored the lead would pass the test above and drop every start but one."""
		first = self._start_kwargs(lead="ZZ-POOL-LEAD-A")
		second = self._start_kwargs(lead="ZZ-POOL-LEAD-B")
		self.assertNotEqual(first["job_id"], second["job_id"])

	def test_the_start_waits_for_the_users_transaction_to_commit(self):
		"""A worker that picks the job up before the save commits reads a lead that is not there yet."""
		self.assertIs(self._start_kwargs().get("enqueue_after_commit"), True)
