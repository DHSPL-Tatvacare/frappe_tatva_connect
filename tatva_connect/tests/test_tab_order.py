# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The three claims `tab_order` makes, each locked so the runtime cannot drift from them.

PERSONAL. The store keys on the session user itself, so one person's arrangement is unreadable to the
next by construction rather than by a filter someone must remember to write.

A HINT, NEVER A FILTER. `apply` returns every row it was given. A view created, shared or unshared
after an arrangement was saved must still appear in the strip, so a name the order does not mention
sorts after the ones it does, and a name that no longer resolves is ignored rather than honoured.

GATED. You may arrange a surface you can open, and nothing else — the check is frappe's own
`has_permission`, not a role list of ours.
"""
import frappe
from frappe.model.utils.user_settings import get_user_settings, update_user_settings
from frappe.tests.utils import FrappeTestCase

from tatva_connect import tab_order

DT = "CRM Smart View"


class TestAnOrderIsAHintNeverAFilter(FrappeTestCase):
	"""`apply` is pure — it reorders what it is handed and never decides membership."""

	def rows(self, *names):
		return [{"name": n} for n in names]

	def test_the_named_rows_come_first_in_the_order_named(self):
		with _arranged(["c", "a"]):
			got = tab_order.apply(self.rows("a", "b", "c"), DT)
		self.assertEqual([r["name"] for r in got], ["c", "a", "b"])

	def test_a_row_the_order_never_mentions_is_kept_not_dropped(self):
		with _arranged(["a"]):
			got = tab_order.apply(self.rows("a", "b"), DT)
		self.assertEqual([r["name"] for r in got], ["a", "b"])

	def test_a_name_that_no_longer_resolves_is_simply_ignored(self):
		with _arranged(["gone", "b"]):
			got = tab_order.apply(self.rows("a", "b"), DT)
		self.assertEqual([r["name"] for r in got], ["b", "a"])

	def test_unmentioned_rows_keep_the_order_they_arrived_in(self):
		with _arranged(["d"]):
			got = tab_order.apply(self.rows("a", "b", "c", "d"), DT)
		self.assertEqual([r["name"] for r in got], ["d", "a", "b", "c"])

	def test_no_arrangement_returns_the_rows_untouched(self):
		with _arranged([]):
			rows = self.rows("a", "b")
			self.assertIs(tab_order.apply(rows, DT), rows)


class TestAnOrderIsPersonal(FrappeTestCase):
	def tearDown(self):
		frappe.set_user("Administrator")

	def test_one_persons_arrangement_is_not_the_next_persons(self):
		other = _user("zz-tab-order-peer@example.com", "Peer")
		tab_order.save_order(DT, ["a", "b"])
		self.assertEqual(tab_order.get_order(DT), ["a", "b"])

		frappe.set_user(other)
		self.assertEqual(tab_order.get_order(DT), [])

		frappe.set_user("Administrator")
		self.assertEqual(tab_order.get_order(DT), ["a", "b"])
		tab_order.save_order(DT, [])

	def test_saving_replaces_wholesale_because_an_order_is_one_fact(self):
		tab_order.save_order(DT, ["a", "b", "c"])
		tab_order.save_order(DT, ["c"])
		self.assertEqual(tab_order.get_order(DT), ["c"])
		tab_order.save_order(DT, [])

	def test_resetting_leaves_no_arrangement_at_all(self):
		tab_order.save_order(DT, ["a"])
		tab_order.save_order(DT, [])
		self.assertEqual(tab_order.get_order(DT), [])

	def test_a_neighbouring_user_setting_survives_the_write(self):
		update_user_settings(DT, {"zz_neighbour": 7})
		tab_order.save_order(DT, ["a"])
		self.assertEqual(frappe.parse_json(get_user_settings(DT)).get("zz_neighbour"), 7)
		tab_order.save_order(DT, [])


class TestTheGate(FrappeTestCase):
	def tearDown(self):
		frappe.set_user("Administrator")

	def test_a_surface_must_be_named(self):
		self.assertRaises(frappe.ValidationError, tab_order.save_order, "", ["a"])

	def test_a_user_who_cannot_read_the_surface_cannot_arrange_it(self):
		frappe.set_user("Guest")
		self.assertRaises(frappe.PermissionError, tab_order.save_order, DT, ["a"])

	def test_an_order_must_be_a_list(self):
		self.assertRaises(frappe.ValidationError, tab_order.save_order, DT, '{"a": 1}')

	def test_a_json_string_order_is_accepted_because_that_is_what_the_browser_sends(self):
		tab_order.save_order(DT, '["a", "b"]')
		self.assertEqual(tab_order.get_order(DT), ["a", "b"])
		tab_order.save_order(DT, [])


class _arranged:
	"""This person, arranged that way, for the length of the block — and back to nothing after."""

	def __init__(self, order):
		self.order = order

	def __enter__(self):
		update_user_settings(DT, {tab_order.KEY: self.order})

	def __exit__(self, *exc):
		update_user_settings(DT, {tab_order.KEY: []})


def _user(email, first_name):
	if not frappe.db.exists("User", email):
		frappe.get_doc({
			"doctype": "User", "email": email, "first_name": first_name, "send_welcome_email": 0,
		}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no session user
	return email
