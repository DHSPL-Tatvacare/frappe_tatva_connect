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
  3. A save BOOKS its start inside its own transaction, under its own savepoint (`triggers._book_start`):
     several saves inside one request book one journey, and a save that rolls back books none.

WHAT EACH TEST READS, AND HOW HARD.

  * The unique key is asserted TWICE, because the two checks are not equally strong. The DocType's `unique`
    flag is the DECLARATION only — it says what model sync intends, not what the table carries. The
    behavioural test is the real one: it inserts a duplicate and requires the DATABASE to refuse it, which
    proves the constraint without naming the index (frappe picks that name, and guessing it here would make
    this lock go red on a rename rather than on a regression).
  * The claims are read off the AST of each module's source, never counted in the text: `for_update=True`
    appears in these modules' own docstrings, so a string count stays green with every real claim deleted.
  * The booking is asserted on the journey rows it really leaves, live or rolled back.

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
from tatva_connect.workflow_engine import interpreter, signals, triggers, versions, wakeups
from tatva_connect.workflow_engine.tests import fixtures as fx

JOURNEY_DT = fx.JOURNEY_DT
_WF = "ZZ Worker Pool Safety"

# The claim sites, by module and count. A worker acts on a row only after taking its lock, so removing one
# of these is how the engine silently stops being pool-safe.
_CLAIMS = ((interpreter, 2), (signals, 1), (wakeups, 1))


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


class TestAStartIsBookedOnceInsideTheSave(FrappeTestCase):
	"""GUARD 3. A save books its start in its own transaction: once per lead however often it saves, and never one that outlives a rolled-back save."""

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

	def _book(self, lead=None):
		with patch("tatva_connect.workflow_engine.drain.pull_forward") as pull_forward:
			triggers._book_start(_WF, versions.current_name(_WF), lead or self.lead.name, {})
		return pull_forward

	def _live(self, lead=None):
		return frappe.get_all(JOURNEY_DT, filters={"workflow": _WF, "subject_name": lead or self.lead.name,
		                                           "status": ["in", interpreter.LIVE_STATES]}, fields=["status", "resume_at"])

	def test_two_saves_of_one_lead_book_one_journey_due_now_for_the_drain(self):
		first, second = self._book(), self._book()
		live = self._live()
		self.assertEqual(len(live), 1, "a second save booked a second journey on the same lead")
		self.assertEqual(live[0].status, "Parked")
		self.assertLessEqual(live[0].resume_at, frappe.utils.now_datetime())
		first.assert_called_once()
		second.assert_not_called()  # nothing new was booked, so the drain is not asked again

	def test_two_leads_both_get_their_journey(self):
		"""The control: a key that ignored the lead would pass the test above and drop every start but one."""
		other = fx.make_lead()
		self._book()
		self._book(other.name)
		self.assertEqual((len(self._live()), len(self._live(other.name))), (1, 1))

	def test_a_save_that_rolls_back_leaves_no_journey(self):
		frappe.db.savepoint("tc_the_save")
		self._book()
		frappe.db.rollback(save_point="tc_the_save")
		self.assertEqual(self._live(), [])
