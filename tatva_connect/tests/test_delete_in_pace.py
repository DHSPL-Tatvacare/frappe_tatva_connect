# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""`utils.delete_in_pace`: waits for the cleanup lane, commits per record, and returns what is still there."""
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect import utils


def _todo(text):
	doc = frappe.get_doc({"doctype": "ToDo", "description": f"zz-delete-in-pace {text}"})
	doc.insert(ignore_permissions=True)
	frappe.db.commit()  # the helper commits and rolls back per record, as its callers' own work already is
	return doc.name


class TestDeleteInPace(FrappeTestCase):
	def tearDown(self):
		for name in frappe.get_all("ToDo", filters={"description": ["like", "zz-delete-in-pace%"]}, pluck="name"):
			frappe.delete_doc("ToDo", name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def test_it_waits_while_the_cleanup_lane_is_busy(self):
		name = _todo("wait")
		with patch.object(utils, "lane_has_room", side_effect=[False, True]) as room, \
		     patch.object(utils.time, "sleep") as sleep:
			left = utils.delete_in_pace("ToDo", [name], lambda n: frappe.delete_doc("ToDo", n, ignore_permissions=True))
		self.assertEqual((left, room.call_args.args, sleep.call_count), ([], (utils.DELETE_LANE,), 1))
		self.assertFalse(frappe.db.exists("ToDo", name))

	def test_it_returns_only_what_is_still_there(self):
		kept, gone = _todo("kept"), _todo("gone")
		frappe.delete_doc("ToDo", gone, ignore_permissions=True)
		frappe.db.commit()

		def delete_one(name):
			raise frappe.ValidationError("held")  # a refused delete and an already-gone record both raise

		with patch.object(utils, "lane_has_room", return_value=True):
			left = utils.delete_in_pace("ToDo", [kept, gone], delete_one)
		self.assertEqual(left, [kept])
