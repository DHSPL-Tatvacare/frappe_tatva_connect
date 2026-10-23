"""Notifications health counts what Notification Log holds: a new notice moves the 7-day card, its type row, and its push outcome."""
import frappe
from frappe.tests import IntegrationTestCase

from tatva_connect.observability import notifications_health as health
from tatva_connect.tests.authz.base import mk_user

TYPE = "Share"


def _notice(user):
	return frappe.get_doc({"doctype": "Notification Log", "for_user": user, "type": TYPE, "subject": "zz health probe"}).insert(ignore_permissions=True)


class TestNotificationsHealth(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.user = mk_user("zz.notify.health@example.com", ["Sales User"])

	def _measure(self, measure):
		frappe.cache.delete_value(health._CACHE_KEY.format(health.WINDOW_DAYS))
		return health.card(frappe.as_json({"measure": measure}))["value"]

	def test_a_new_notice_counts_on_the_card_the_type_row_and_the_push_chart(self):
		before, row_before = self._measure("written_7d"), {r["type"]: r for r in health.by_type()}.get(TYPE, {"written": 0})
		unread_before = self._measure("unread_30d")
		_notice(self.user)
		self.assertEqual(self._measure("written_7d"), before + 1)
		self.assertEqual(self._measure("unread_30d"), unread_before + 1)
		self.assertEqual({r["type"]: r for r in health.by_type()}[TYPE]["written"], row_before["written"] + 1)
		self.assertIn(health.NOT_REACHED, [d["name"] for d in health.daily("push")["datasets"]])

	def test_a_non_manager_is_refused(self):
		frappe.set_user(self.user)
		with self.assertRaises(frappe.PermissionError):
			health.card(frappe.as_json({"measure": "written_7d"}))
