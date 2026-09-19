# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Check against Meta: the four buckets, the log's lead id, and a re-sync that folds only what the last check found missing."""
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.lead_sync import reconcile
from tatva_connect.lead_sync.failure_log import lead_id_of

FORM = "zz-check-form"


class TestCompare(FrappeTestCase):
	def test_every_meta_lead_lands_in_exactly_one_bucket(self):
		meta = {"a": "2026-09-18 10:00:00", "b": "2026-09-18 11:00:00", "c": "2026-09-18 12:00:00"}
		out = reconcile.compare(meta, in_crm={"a"}, failed={"b": ("LOG-1", "Failure")})
		self.assertEqual((out["meta"], out["in_crm"], out["failed"], out["missing"]), (3, 1, 1, 1))
		self.assertEqual([(r["lead_id"], r["state"], r["log"]) for r in out["rows"]],
		                 [("c", "Missing", None), ("b", "Failed", "LOG-1")])

	def test_a_lead_in_the_crm_is_never_listed_even_with_an_old_log(self):
		out = reconcile.compare({"a": None}, in_crm={"a"}, failed={"a": ("LOG-9", "Failure")})
		self.assertEqual((out["in_crm"], out["rows"]), (1, []))

	def test_the_log_names_its_lead_in_either_shape(self):
		self.assertEqual(lead_id_of('{"facebook_lead_id": "1"}'), "1")
		self.assertEqual(lead_id_of('{"id": "2"}'), "2")
		self.assertIsNone(lead_id_of(None))


class TestResync(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		frappe.cache.delete_value(reconcile._missing_key(FORM))

	def tearDown(self):
		frappe.cache.delete_value(reconcile._missing_key(FORM))

	def _run(self, held):
		fold = MagicMock()
		with patch.object(reconcile, "_sources", return_value=["S"]), \
		     patch.object(reconcile.frappe, "get_doc", return_value=frappe._dict(name="S")), \
		     patch.object(reconcile, "fold_for", return_value=fold), \
		     patch.object(reconcile, "_in_crm", side_effect=lambda ids: held & set(ids)):
			return reconcile.resync(FORM), fold

	def test_without_a_recent_check_nothing_is_folded(self):
		with self.assertRaises(frappe.ValidationError):
			self._run(set())

	def test_only_the_checked_ids_still_missing_are_fetched(self):
		frappe.cache.set_value(reconcile._missing_key(FORM), ["a", "b"], expires_in_sec=60)
		_result, fold = self._run(held={"a"})
		self.assertEqual([c.args[0] for c in fold.fetch_one_lead.call_args_list], ["b"])
		self.assertIsNone(frappe.cache.get_value(reconcile._missing_key(FORM)), "a spent list was left to replay")
