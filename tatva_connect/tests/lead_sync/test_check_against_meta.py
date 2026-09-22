# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Validation against Meta: the four states a lead can be in, and a re-sync that folds only what that verdict left unlinked.

The state that matters is MISSING, which must mean the sync lost the lead — not merely that a submission
was never stamped onto the person it belongs to. So the bucket tests hold that line: a lead whose phone
already identifies a CRM lead is reported as in the CRM, never as missing.
"""
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.lead_sync import reconcile
from tatva_connect.lead_sync.failure_log import lead_id_of

FORM = "zz-check-form"


def _fact(received="2026-09-18 10:00:00", phone=None):
	return {"received": received, "phone": phone}


class TestCompare(FrappeTestCase):
	def test_every_meta_lead_lands_in_exactly_one_state(self):
		meta = {"a": _fact("2026-09-18 10:00:00"), "b": _fact("2026-09-18 11:00:00"),
		        "c": _fact("2026-09-18 12:00:00"), "d": _fact("2026-09-18 13:00:00", "+919876543210")}
		out = reconcile.compare(meta, linked={"a"}, failed={"b": ("LOG-1", "Failure")}, people={"+919876543210"})
		self.assertEqual((out["meta"], out["linked"], out["failed"], out["in_crm"], out["missing"]), (4, 1, 1, 1, 1))
		self.assertEqual([(r["lead_id"], r["state"]) for r in out["rows"]],
		                 [("d", reconcile.IN_CRM), ("c", reconcile.MISSING), ("b", reconcile.FAILED)])

	def test_a_person_already_in_the_crm_is_not_reported_missing(self):
		out = reconcile.compare({"a": _fact(phone="+919876543210")}, linked=set(), failed={},
		                        people={"+919876543210"})
		self.assertEqual((out["missing"], out["in_crm"]), (0, 1))

	def test_a_lead_nobody_answers_to_is_missing(self):
		out = reconcile.compare({"a": _fact(phone="+910000000000")}, linked=set(), failed={}, people=set())
		self.assertEqual((out["missing"], out["in_crm"]), (1, 0))

	def test_a_linked_lead_is_never_listed_even_with_an_old_log(self):
		out = reconcile.compare({"a": _fact()}, linked={"a"}, failed={"a": ("LOG-9", "Failure")}, people=set())
		self.assertEqual((out["linked"], out["rows"]), (1, []))

	def test_the_log_names_its_lead_in_either_shape(self):
		self.assertEqual(lead_id_of('{"facebook_lead_id": "1"}'), "1")
		self.assertEqual(lead_id_of('{"id": "2"}'), "2")
		self.assertIsNone(lead_id_of(None))


class TestResync(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		frappe.cache.delete_value(reconcile._bucket_key(FORM))

	def tearDown(self):
		frappe.cache.delete_value(reconcile._bucket_key(FORM))

	def _run(self, held):
		fold = MagicMock()
		with patch.object(reconcile, "_sources", return_value=["S"]), \
		     patch.object(reconcile.frappe, "get_doc", return_value=frappe._dict(name="S")), \
		     patch.object(reconcile, "fold_for", return_value=fold), \
		     patch.object(reconcile.frappe, "publish_realtime") as published, \
		     patch.object(reconcile, "_in_crm", side_effect=lambda ids: held & set(ids)):
			reconcile.run_resync(FORM, "Administrator")
			return published, fold

	def test_without_a_recent_verdict_nothing_is_folded(self):
		with self.assertRaises(frappe.ValidationError):
			self._run(set())

	def test_only_the_ids_still_unlinked_are_fetched(self):
		frappe.cache.set_value(reconcile._bucket_key(FORM), ["a", "b"], expires_in_sec=60)
		_published, fold = self._run(held={"a"})
		self.assertEqual([c.args[0] for c in fold.fetch_one_lead.call_args_list], ["b"])
		self.assertIsNone(frappe.cache.get_value(reconcile._bucket_key(FORM)), "a spent verdict was left to replay")

	def test_every_lead_announces_its_progress_and_the_end_reports_the_outcome(self):
		frappe.cache.set_value(reconcile._bucket_key(FORM), ["a", "b"], expires_in_sec=60)
		published, _fold = self._run(held=set())
		events = [(call.args[0], call.args[1]) for call in published.call_args_list]
		self.assertEqual([payload["processed"] for name, payload in events if name == reconcile.EVENT_RESYNC_PROGRESS],
		                 [1, 2])
		self.assertTrue(all(payload["total"] == 2 for name, payload in events
		                    if name == reconcile.EVENT_RESYNC_PROGRESS))
		self.assertEqual([payload for name, payload in events if name == reconcile.EVENT_RESYNC_DONE],
		                 [{"form": FORM, "linked": 0, "failed": 2}])
