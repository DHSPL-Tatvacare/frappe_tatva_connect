# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A pool that requires check-in skips a checked-out rep until they check in; a pool that does not ignores check-in."""

from frappe.tests import set_user

from tatva_connect.api import checkin as checkin_api
from tatva_connect.tests.workflow_engine import fixtures as fx


def _check(user, status):
	with set_user(user):
		checkin_api.set_status(status)


class TestAPoolThatRequiresCheckInSkipsACheckedOutRep(fx.PoolTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.late, cls.early, cls.out = fx.rep("checkin-late"), fx.rep("checkin-early"), fx.rep("checkin-out")
		for user, status in ((cls.late, "Unavailable"), (cls.early, "Active"), (cls.out, "Unavailable")):
			_check(user, status)
		cls.strict = fx.pool([(cls.late, 1, None), (cls.early, 1, None)], require_checkin=1)
		cls.relaxed = fx.pool([(cls.out, 1, None)])
		cls.before, cls.after, cls.relaxed_leads = fx.leads(2), fx.leads(2), fx.leads(2)

	def test_a_checked_out_rep_is_skipped_until_they_check_in(self):
		self.assertEqual({fx.distribute(self.strict, lead)[1] for lead in self.before}, {self.early})
		_check(self.late, "Active")
		self.assertEqual({fx.distribute(self.strict, lead)[1] for lead in self.after}, {self.late, self.early})

	def test_a_pool_that_does_not_require_check_in_ignores_it(self):
		self.assertEqual({fx.distribute(self.relaxed, lead)[1] for lead in self.relaxed_leads}, {self.out})
