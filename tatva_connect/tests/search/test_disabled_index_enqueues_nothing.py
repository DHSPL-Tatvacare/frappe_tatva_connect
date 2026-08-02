"""A dormant search index schedules NOTHING, and asking costs no query.

The toggle used to gate only the work: with `Search::Index::indexing` off every lead write still enqueued
a job, which paid an RQ dequeue and an engine construction before returning, and once the queue reached
frappe's ceiling the enqueue itself raised QueueOverloaded and wrote an Error Log row per save. Measured
during a migration with the switch OFF: two worker containers at ~100% CPU and ~450 error rows a minute,
all of it caused by the writer they were contending with.

The second test is the other half of the instruction: the guard reads the switch on EVERY lead write, so
it must resolve through the cached reader and add no query to a save.
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.search import index

TOGGLE = index.TOGGLE


def _set(enabled):
	frappe.db.set_value("CRM Tatva Automation", TOGGLE, "enabled", 1 if enabled else 0)


class TestDisabledIndexEnqueuesNothing(FrappeTestCase):
	def setUp(self):
		self.was = frappe.db.get_value("CRM Tatva Automation", TOGGLE, "enabled")
		self.enqueued = []
		frappe.local.flags.pop("in_import", None)

	def tearDown(self):
		_set(self.was)
		index._discard_reindex()

	def _collect(self):
		"""Capture what `_flush_reindex` would hand to frappe.enqueue, without a worker or a redis."""
		import unittest.mock as mock
		return mock.patch.object(frappe, "enqueue", side_effect=lambda *a, **k: self.enqueued.append(k))

	def test_switch_off_schedules_nothing(self):
		_set(False)
		with self._collect():
			index._enqueue_reindex("LEAD-PROBE-1")
			index._flush_reindex()
		self.assertEqual(self.enqueued, [], "a dormant index must not enqueue a job")

	def test_switch_on_still_schedules(self):
		"""The guard must not break the feature it protects."""
		_set(True)
		with self._collect():
			index._enqueue_reindex("LEAD-PROBE-2")
			index._flush_reindex()
		self.assertEqual(len(self.enqueued), 1, "with the switch on the job must still be enqueued")
		self.assertEqual(self.enqueued[0].get("lead"), "LEAD-PROBE-2")

	def test_the_guard_costs_no_query(self):
		"""It runs on every lead write, so an uncached read here is a SELECT added to every save.

		Counted at the driver, not inferred: the cursor is wrapped and every statement the guard issues is
		counted. `is_enabled` resolves through `frappe.get_cached_value`, so a warm cache answers all 20."""
		_set(False)
		index.is_enabled(TOGGLE)  # warm it exactly as the first save of a request would have

		cursor_cls = frappe.db._cursor.__class__
		real, seen = cursor_cls.execute, []

		def spy(cursor, query, args=None):
			seen.append(str(query))
			return real(cursor, query, args)

		cursor_cls.execute = spy
		try:
			with self._collect():
				for i in range(20):
					index._enqueue_reindex(f"LEAD-PROBE-{i}")
		finally:
			cursor_cls.execute = real

		self.assertEqual(seen, [], f"20 writes must add no query; the guard issued {len(seen)}")
