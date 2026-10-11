# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A pool skips a member on leave in its grain, keeps one on leave elsewhere, reads leave from frappe's cache, and sees a new leave on the next draw."""

from unittest.mock import patch

import frappe
from frappe.tests import freeze_time

from tatva_connect.lead import assignment_rule
from tatva_connect.tests.workflow_engine import fixtures as fx


class TestAPoolReadsItsMembersLeaveOnce(fx.PoolTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.away, cls.elsewhere, cls.here = fx.rep("leave-away"), fx.rep("leave-elsewhere"), fx.rep("leave-here")
		cls.rule = fx.pool([(cls.away, 10, None), (cls.elsewhere, 10, None), (cls.here, 10, None)])
		fx.leave(cls.away, fx.MONDAY)
		frappe.get_doc({
			"doctype": "Tatva User Leave", "user": cls.elsewhere, "from_date": fx.MONDAY, "to_date": fx.MONDAY,
			"grain_vertical": fx.OTHER_GRAIN["vertical"],
		}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture

	def _drawn(self, count):
		with freeze_time(fx.at(fx.MONDAY, 10)):
			return {fx.distribute(self.rule, lead)[1] for lead in fx.leads(count)}

	def test_only_leave_in_the_pools_grain_takes_a_member_out(self):
		self.assertEqual(self._drawn(6), {self.elsewhere, self.here})

	def test_a_warm_draw_reads_no_leave_from_the_database(self):
		self._drawn(1)
		real_sql, leave_reads = frappe.db.sql, []

		def counting(query, *args, **kwargs):
			if "Tatva User Leave" in str(query):
				leave_reads.append(query)
			return real_sql(query, *args, **kwargs)

		with patch.object(frappe.db, "sql", side_effect=counting):
			self._drawn(1)
		self.assertEqual(leave_reads, [], "the pool's leave comes from frappe's cache once it is warm")

	def test_a_leave_saved_or_deleted_in_the_desk_is_seen_on_the_next_draw(self):
		self._drawn(1)
		spell = fx.leave(self.here, fx.MONDAY)
		self.assertEqual(self._drawn(4), {self.elsewhere}, "saving the leave must drop the cached map")
		spell.delete(ignore_permissions=True)  # authz-ok: tier-c — test fixture
		self.assertEqual(self._drawn(4), {self.elsewhere, self.here}, "deleting the leave must drop it too")

	def test_a_map_built_before_a_delete_neither_breaks_a_draw_nor_tells_the_user(self):
		spell = fx.leave(self.here, fx.MONDAY)
		self._drawn(1)
		self.addCleanup(assignment_rule.clear_pool_map, spell)  # the stale map must not outlive this test
		with patch.object(assignment_rule, "clear_pool_map"):  # an older snapshot rebuilt the map after the delete cleared it
			spell.delete(ignore_permissions=True)  # authz-ok: tier-c — test fixture
		frappe.local.message_log = []
		self.assertTrue(self._drawn(4), "a map that names a deleted leave must still draw")
		self.assertEqual(frappe.local.message_log, [], "a gone row must not surface as a toast on a lead save")
