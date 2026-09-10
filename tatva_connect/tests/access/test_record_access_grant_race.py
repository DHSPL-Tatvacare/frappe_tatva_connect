# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Two savers granting the same person the same lead must not kill each other.

THE PRODUCTION FAILURE. `sync` reads which grants exist, works out who is missing, and writes them. Two
transactions touching one lead at the same moment — a rep's save and a workflow effect — cannot see each
other's uncommitted insert, so both legitimately conclude the row is absent and both write it. The second
died on the unique index `ix_record_access_user_ref` with `UniqueValidationError`, and because `sync` runs
inline inside the caller, it took the automation that was mid-flight down with it: the next task never
raised, the stage never moved, nothing visible anywhere but the Error Log.

WHY THE RACE IS FORCED, NOT HOPED FOR. Two real connections (frappe.local is thread-local, so a thread
without its own connection would share this test's transaction and race nothing) meet at a barrier placed
between the read and the write — the exact window the bug lives in.

RED IS PROVEN, NOT ASSUMED, AND ON ONE CONDITION. `test_the_old_document_insert_raises` asserts the
production error by name AND the index by name. Not "raised or lost a row" — a disjunction is how the
previous attempt at this class of bug went green on the wrong symptom while the real one kept firing.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.access.test_record_access_grant_race
"""
import threading

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.access import record_access
from tatva_connect.workflow_engine.tests import fixtures as fx

_INDEX = "ix_record_access_user_ref"


class _GrantRaceBase(FrappeTestCase):
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
			except Exception as e:  # collected, never swallowed — a thread that died quietly proves nothing
				raised.append(e)
			finally:
				frappe.destroy()

		threads = [threading.Thread(target=run) for _ in range(2)]
		for t in threads:
			t.start()
		for t in threads:
			t.join(timeout=60)
		# THIS CONNECTION'S read view was opened in setUp and cannot see what the threads committed after
		# it — the same snapshot isolation this suite exists for. Without it the assertions below read zero
		# rows, and tearDown's DELETE raises 1020 against rows it cannot see.
		frappe.db.commit()
		return raised

	def _rows(self):
		return frappe.db.count(record_access.DOCTYPE,
		                       {"user": self.user, "reference_doctype": "CRM Lead", "reference_name": self.lead.name})


class TestTheOldGrantWriteRaces(_GrantRaceBase):
	"""THE PREMISE. The document insert `sync` used to make, driven through the same forced race. If this
	ever stops raising, the bug being fixed was not the bug in production and this file is worthless."""

	def test_the_old_document_insert_raises_unique_validation_on_the_named_index(self):
		def old_write(barrier):
			frappe.db.exists(record_access.DOCTYPE,  # the read half of the read-then-write
			                 {"user": self.user, "reference_doctype": "CRM Lead", "reference_name": self.lead.name})
			barrier.wait()
			frappe.get_doc({"doctype": record_access.DOCTYPE, "user": self.user,
			                "reference_doctype": "CRM Lead", "reference_name": self.lead.name}
			               ).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input

		raised = self._race(old_write)
		self.assertEqual(len(raised), 1, f"exactly one writer must lose the race; got {raised!r}")
		self.assertIsInstance(raised[0], frappe.UniqueValidationError,
		                      f"production reports UniqueValidationError; this raised {type(raised[0]).__name__}")
		self.assertIn(_INDEX, str(raised[0]), f"the collision must be on {_INDEX}, the index production names")


class TestTheSharedGrantWriteSurvives(_GrantRaceBase):
	"""THE FIX, through the REAL `sync` — not a copy of it. The barrier is placed on `_grant`, which lands
	it exactly between the read `sync` has already done and the write it is about to make."""

	def test_two_concurrent_syncs_both_succeed_and_leave_one_row(self):
		# Patched ONCE, around the whole race: patching inside the threads lets the first finisher restore
		# the original while the second is still short of the barrier, and the race then never happens.
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
