# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Two saves that grant the same person the same lead at once both succeed and leave one grant row.
Two real connections meet at a barrier between the read and the write, so the race is forced."""
import threading

import frappe
from frappe.tests import IntegrationTestCase

from tatva_connect.access import record_access
from tatva_connect.workflow_engine.tests import fixtures as fx

_INDEX = "ix_record_access_user_ref"


class _GrantRaceBase(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		# The owner is passed AT INSERT: `viewers` reads it off the lead, and an unowned lead grants nobody.
		self.user = frappe.session.user
		self.lead = fx.make_lead(**{record_access.SUBJECTS["CRM Lead"]: self.user})
		frappe.db.delete(record_access.DOCTYPE, {"reference_name": self.lead.name})
		frappe.db.commit()

	def tearDown(self):
		frappe.db.delete(record_access.DOCTYPE, {"reference_name": self.lead.name})
		frappe.db.delete("CRM Lead", {"name": self.lead.name})
		frappe.db.commit()
		super().tearDown()

	def _race(self, body):
		"""Run `body(barrier)` on two real connections, each in its own transaction."""
		site, raised = frappe.local.site, []
		barrier = threading.Barrier(2, timeout=10)

		def run():
			frappe.init(site=site)
			frappe.connect()
			try:
				frappe.db.begin()
				body(barrier)
				frappe.db.commit()
			except Exception as e:  # collected so a thread that dies quietly still fails the test
				raised.append(e)
			finally:
				frappe.destroy()

		threads = [threading.Thread(target=run) for _ in range(2)]
		for t in threads:
			t.start()
		for t in threads:
			t.join(timeout=60)
		# Refresh this connection's snapshot, or it cannot see the rows the threads committed.
		frappe.db.commit()
		return raised

	def _rows(self):
		return frappe.db.count(record_access.DOCTYPE,
		                       {"user": self.user, "reference_doctype": "CRM Lead", "reference_name": self.lead.name})


class TestTheSharedGrantWriteSurvives(_GrantRaceBase):
	"""Drives the real `sync`, with the barrier on `_grant` so it lands between the read and the write."""

	def test_two_concurrent_syncs_both_succeed_and_leave_one_row(self):
		# Patch once around the whole race; patching per thread lets the first finisher restore it too early.
		real_grant = record_access._grant
		holder = {}

		def barriered(doctype, pairs):
			holder["barrier"].wait()
			return real_grant(doctype, pairs)

		def run_sync(barrier):
			holder["barrier"] = barrier
			record_access.sync("CRM Lead", self.lead.name)

		record_access._grant = barriered
		try:
			raised = self._race(run_sync)
		finally:
			record_access._grant = real_grant
		self.assertEqual(raised, [], f"a concurrent grant raised: {raised!r}")
		self.assertEqual(self._rows(), 1, "the grant must exist exactly once — not zero, not twice")
