# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Opening a journey must not write the parent workflow row.

WHY THIS IS A LOCK AND NOT A UNIT TEST. `open_journey` used to stamp `last_journey_at` and
`journeys_started` on `tabCRM Workflow`. Under MariaDB 11.6+ (`innodb_snapshot_isolation=ON`, which prod
runs) a LOCKING write to a row that another transaction has committed since this transaction's read view
opened is refused outright with 1020 "Record has changed since last read". The cohort drainer writes that
same row once per lead, so it is always moving — and ~600 ephemeral runs died on it, each one a task never
raised and a stage never moved, visible nowhere but the Error Log.

The first attempt at this collapsed the read-then-write into one atomic UPDATE and asserted the counter no
longer lost increments. That is a real bug and a real fix, but it is not this one: the statement stayed,
and the statement is what raises. This asserts the STATEMENT IS GONE, which is the only thing that makes
1020-from-this-path impossible rather than merely rarer. An assertion about a missing statement cannot
quietly pass on the wrong symptom the way an assertion about a symptom did.

`tabCRM Workflow` is matched backticked and exact, so the child tables (`tabCRM Workflow Journey`,
`... Node`, `... Step Log`) — which this path SHOULD write — do not satisfy it.
"""
import re
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.tests.authz.grains import assert_masters_exist
from tatva_connect.workflow_engine import interpreter, versions
from tatva_connect.workflow_engine.tests import fixtures as fx

_INLINE = "parent-untouched-inline"

# The parent table, backticked and exact — `tabCRM Workflow Journey` must NOT match.
_PARENT = re.compile(r"`tabCRM Workflow`")
_WRITES = re.compile(r"^\s*(update|insert|replace|delete)\b", re.I)


def _parent_writes(calls):
	"""Every statement the spy saw that WRITES the parent workflow row."""
	seen = []
	for call in calls:
		sql = str(call.args[0]) if call.args else ""
		if _WRITES.match(sql) and _PARENT.search(sql):
			seen.append(" ".join(sql.split())[:200])
	return seen


class TestOpenJourneyTouchesNoParent(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		fx.purge(_INLINE)
		fx.arm_engine(True, cls)
		# Wait-free: the front door routes this to the EPHEMERAL lane (`triggers._run_ephemeral`).
		cls.inline = fx.make_workflow(_INLINE, [
			fx.trigger(to="end"),
			fx.node("end", "Terminal"),
		])
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		fx.purge(_INLINE)
		frappe.db.commit()

	def test_open_journey_writes_nothing_to_the_parent_workflow_row(self):
		"""The direct, deterministic form: call the shared opener and watch the statements."""
		lead = fx.make_lead()
		version = versions.current_name(self.inline.name)
		with patch.object(frappe.db, "sql", wraps=frappe.db.sql) as spy:
			journey = interpreter.open_journey(self.inline.name, version, lead.name, {})
		self.assertTrue(frappe.db.exists("CRM Workflow Journey", journey.name),
		                "the journey must still be recorded — this removes a counter, not the record")
		self.assertEqual(_parent_writes(spy.mock_calls), [],
		                 "open_journey wrote the parent workflow row; that write raises 1020 under snapshot isolation")

	def test_a_real_ephemeral_run_writes_nothing_to_the_parent_workflow_row(self):
		"""The whole front door, the way a rep's save reaches it — `doc_events` → `_maybe_start` → inline."""
		with patch.object(frappe.db, "sql", wraps=frappe.db.sql) as spy:
			lead = fx.make_lead()
		self.assertTrue(
			frappe.db.exists("CRM Workflow Journey", {"workflow": self.inline.name, "subject_name": lead.name}),
			"the ephemeral run must still have happened — otherwise this test proves nothing",
		)
		self.assertEqual(_parent_writes(spy.mock_calls), [],
		                 "a rep's save wrote the parent workflow row; that is the 1020 path")
