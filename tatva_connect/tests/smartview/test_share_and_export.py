# Copyright (c) 2026, TatvaCare and Contributors. See license.txt
"""Sharing rides DocShare and export re-runs the composer; neither widens rows or columns past the permission model."""
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect import exports
from tatva_connect.smartview import api as smartview

LABEL = "ZZ Share View"
OWNER = "zz-share-owner@example.com"
FRIEND = "zz-share-friend@example.com"
STRANGER = "zz-share-stranger@example.com"


class _ShareCase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		for email, name in ((OWNER, "Share Owner"), (FRIEND, "Share Friend"), (STRANGER, "Share Stranger")):
			if not frappe.db.exists("User", email):
				frappe.get_doc({"doctype": "User", "email": email, "first_name": name,
				                "send_welcome_email": 0, "roles": [{"role": "Sales User"}]}
				               ).insert(ignore_permissions=True)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		for email in (OWNER, FRIEND, STRANGER):
			if frappe.db.exists("User", email):
				frappe.delete_doc("User", email, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		frappe.set_user("Administrator")
		self._purge()
		cat = smartview.field_catalog("Lead")
		self.view = frappe.get_doc({
			"doctype": "CRM Smart View", "label": LABEL, "base_object": "Lead",
			"is_standard": 0, "owner_user": OWNER,
			"columns": frappe.as_json([c["field_key"] for c in cat[:2]]),
		}).insert(ignore_permissions=True).name
		frappe.db.commit()

	def tearDown(self):
		frappe.set_user("Administrator")
		self._purge()
		frappe.db.commit()

	def _purge(self):
		for name in frappe.get_all("CRM Smart View", filters={"label": LABEL}, pluck="name"):
			frappe.db.delete("DocShare", {"share_doctype": "CRM Smart View", "share_name": name})
			frappe.delete_doc("CRM Smart View", name, force=True, ignore_permissions=True)

	def _tabs_for(self, user):
		frappe.set_user(user)
		try:
			return [t["name"] for t in smartview.get_smart_views()]
		finally:
			frappe.set_user("Administrator")


DT = "CRM Smart View"


class TestSharingIsDocShare(_ShareCase):
	"""Sharing is frappe's own: `frappe.share.add` / `set_permission`, as Desk calls them. No wrapper, no second table."""

	def _share(self, by, user=None, **rights):
		frappe.set_user(by)
		try:
			return frappe.share.add(DT, self.view, user, **rights)
		finally:
			frappe.set_user("Administrator")

	def _set(self, by, user, right, value):
		frappe.set_user(by)
		try:
			return frappe.share.set_permission(DT, self.view, user, right, value)
		finally:
			frappe.set_user("Administrator")

	def test_a_private_view_reaches_only_its_owner(self):
		self.assertIn(self.view, self._tabs_for(OWNER))
		self.assertNotIn(self.view, self._tabs_for(STRANGER))

	def test_the_owner_shares_through_frappe_and_it_reaches_only_that_person(self):
		self._share(OWNER, FRIEND, read=1)
		self.assertTrue(frappe.db.exists("DocShare", {"share_doctype": DT, "share_name": self.view, "user": FRIEND}))
		self.assertIn(self.view, self._tabs_for(FRIEND))
		self.assertNotIn(self.view, self._tabs_for(STRANGER), "a share reached someone it was not given to")

	def test_the_owner_takes_a_share_back(self):
		self._share(OWNER, FRIEND, read=1)
		self._set(OWNER, FRIEND, "read", 0)
		self.assertNotIn(self.view, self._tabs_for(FRIEND))

	def test_a_shared_view_can_be_opened_and_a_stranger_cannot(self):
		self._share(OWNER, FRIEND, read=1)
		frappe.set_user(FRIEND)
		try:
			self.assertEqual(smartview.get_view(self.view)["name"], self.view)
		finally:
			frappe.set_user("Administrator")
		frappe.set_user(STRANGER)
		try:
			with self.assertRaises(frappe.PermissionError):
				smartview.get_view(self.view)
		finally:
			frappe.set_user("Administrator")

	def test_a_reader_cannot_hand_it_on(self):
		self._share(OWNER, FRIEND, read=1)
		with self.assertRaises(frappe.PermissionError):
			self._share(FRIEND, STRANGER, read=1)
		self.assertNotIn(self.view, self._tabs_for(STRANGER))

	def test_nobody_grants_a_right_they_do_not_hold(self):
		"""Frappe's check_share_permission: FRIEND may share, but holds no write, so cannot grant write."""
		self._share(OWNER, FRIEND, read=1, share=1)
		with self.assertRaises(frappe.PermissionError):
			self._share(FRIEND, STRANGER, read=1, write=1)

	def test_a_write_share_edits_through_the_spa_and_through_the_document(self):
		self._share(OWNER, FRIEND, read=1, write=1)
		frappe.set_user(FRIEND)
		try:
			self.assertTrue(smartview.get_view(self.view)["can_write"])
			self.assertTrue(smartview.set_column_widths(self.view, {})["saved"])
			doc = frappe.get_doc(DT, self.view)
			doc.description = "edited through a write share"
			doc.save()
		finally:
			frappe.set_user("Administrator")

	def test_a_share_never_grants_delete(self):
		self._share(OWNER, FRIEND, read=1, write=1, share=1)
		frappe.set_user(FRIEND)
		try:
			with self.assertRaises(frappe.PermissionError):
				smartview.delete_view(self.view)
			self.assertFalse(frappe.has_permission(DT, "delete", doc=self.view))
		finally:
			frappe.set_user("Administrator")

	def test_everyone_reaches_a_stranger(self):
		self._share(OWNER, everyone=1, read=1)
		self.assertIn(self.view, self._tabs_for(STRANGER))

	def test_the_owner_offers_it_to_the_grain_and_takes_it_back(self):
		"""Everyone in the view's grain: the one reach a DocShare cannot say. The owner stays the owner."""
		frappe.set_user(OWNER)
		try:
			smartview.set_public(self.view, 1)
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value(DT, self.view, "owner_user"), OWNER, "publishing must not disown the view")
		self.assertIn(self.view, self._tabs_for(STRANGER), "a grain-wide view did not reach the grain")
		frappe.set_user(OWNER)
		try:
			smartview.set_public(self.view, 0)
		finally:
			frappe.set_user("Administrator")
		self.assertNotIn(self.view, self._tabs_for(STRANGER))

	def test_a_reader_cannot_offer_it_to_the_grain(self):
		self._share(OWNER, FRIEND, read=1, write=1)
		frappe.set_user(FRIEND)
		try:
			with self.assertRaises(frappe.PermissionError):
				smartview.set_public(self.view, 1)
		finally:
			frappe.set_user("Administrator")

	def test_a_write_share_cannot_take_the_view_or_widen_its_reach(self):
		"""Editing is shared; owning and reaching the business line are not."""
		self._share(OWNER, FRIEND, read=1, write=1)
		frappe.set_user(FRIEND)
		try:
			for field, value in (("owner_user", FRIEND), ("is_standard", 1)):
				doc = frappe.get_doc(DT, self.view)
				doc.set(field, value)
				with self.assertRaises(frappe.PermissionError):
					doc.save()
		finally:
			frappe.set_user("Administrator")

	def test_only_a_sharer_may_search_who_to_share_with(self):
		self._share(OWNER, FRIEND, read=1)
		frappe.set_user(FRIEND)
		try:
			with self.assertRaises(frappe.PermissionError):
				smartview.share_user_query("User", "", "name", 0, 20, {"view": self.view})
		finally:
			frappe.set_user("Administrator")

	def test_our_answers_are_frappes_answers(self):
		"""`can_write` / `can_share` are the tab-row shortcut for `frappe.has_permission`; they must never disagree."""
		from tatva_connect.smartview import permissions as sv_perms
		self._share(OWNER, FRIEND, read=1, write=1)
		doc = frappe.get_doc(DT, self.view)
		for user in (OWNER, FRIEND, STRANGER, "Administrator"):
			for right, ours in (("write", sv_perms.can_write), ("share", sv_perms.can_share)):
				native = bool(frappe.has_permission(DT, right, doc=doc, user=user))
				self.assertEqual(ours(doc, user), native, f"{right} for {user}")


class TestExportIsTheScreenAsAFile(_ShareCase):
	"""An export re-runs the composer. It cannot widen rows, and it cannot widen columns."""

	def test_it_needs_the_native_export_permission(self):
		"""Not a permission invented here — the ordinary role flag an operator ticks on the doctype."""
		with patch("frappe.permissions.can_export", return_value=False):
			with self.assertRaises(frappe.PermissionError):
				smartview.export_view(self.view, "csv")

	def test_the_button_is_offered_on_the_same_answer_it_enforces(self):
		with patch("frappe.permissions.can_export", return_value=False):
			self.assertFalse(smartview.can_export("Lead"))
		with patch("frappe.permissions.can_export", return_value=True):
			self.assertTrue(smartview.can_export("Lead"))

	# Rows, columns and the audit row live in `produce_export`, so these drive the producer directly.
	def _produce(self, fmt="csv", **params):
		"""Run the producer as the worker does, with a stub job carrying the two fields it reads."""
		job = frappe._dict(reference=self.view, fmt=fmt)
		return smartview.produce_export(job, params, lambda rows: None)

	def test_it_re_runs_get_data_rather_than_querying_again(self):
		"""Every page of an export comes through `get_data` for this view."""
		with patch.object(smartview, "get_data", wraps=smartview.get_data) as composer:
			self._produce()
		# At least once and always for this view; a large view is walked page by page.
		self.assertTrue(composer.called)
		self.assertTrue(all(c.args[0] == self.view for c in composer.call_args_list))

	def test_the_file_carries_the_views_own_columns(self):
		with patch.object(smartview.tabular, "write", wraps=smartview.tabular.write) as write:
			self._produce()
		header = write.call_args.args[0]
		labels = [c["label"] for c in smartview.get_data(self.view, page_size=1)["columns"]]
		self.assertEqual(header, labels, "the download's header is not the view's own columns")

	def test_a_filter_narrows_the_download_too(self):
		"""The export takes the SAME filters the screen has, so a filtered list downloads filtered."""
		cat = {c["field_key"]: c for c in smartview.field_catalog("Lead")}
		key = next(k for k, c in cat.items() if c["filterable"] and c["fieldtype"] == "Data")
		with patch.object(smartview.tabular, "write", wraps=smartview.tabular.write) as write:
			self._produce(filters=[[key, "=", "zz-no-lead-carries-this"]])
		self.assertEqual(write.call_args.args[1], [], "a filter did not narrow the download")

	def test_it_goes_through_the_one_file_door(self):
		"""The file is written by `tabular.write` in the requested format."""
		with patch.object(smartview.tabular, "write", wraps=smartview.tabular.write) as write:
			made = self._produce("xlsx")
		self.assertEqual(write.call_args.args[2], "xlsx")
		self.assertEqual(made["ext"], "xlsx")

	def test_it_exports_every_row_not_just_the_first_page(self):
		"""An export walks every PAGE_MAX window, never stopping at the first — a dropped window is invisible in the file."""
		total = smartview.get_data(self.view, page_size=1)["total"]
		if total <= smartview.PAGE_MAX:
			self.skipTest(f"only {total} rows on this bench — a single page cannot prove paging")
		exported = self._produce()["rows"]
		self.assertGreater(exported, smartview.PAGE_MAX,
		                   f"the export stopped at one page ({exported} rows) of {total}")
		self.assertEqual(exported, min(total, exports.row_cap()))

	def test_every_download_is_logged(self):
		"""Producing the file writes an Access Log row."""
		before = frappe.db.count("Access Log")
		self._produce()
		self.assertGreater(frappe.db.count("Access Log"), before, "the download left no audit trail")

	def test_asking_for_it_queues_a_job_rather_than_building_it_inline(self):
		"""The endpoint returns a queued job owned by the caller and never touches the file writer."""
		with patch.object(smartview.tabular, "write") as write:
			queued = smartview.export_view(self.view, "csv")
		write.assert_not_called()
		self.assertTrue(queued.get("job"), "the endpoint did not return a job to wait for")
		self.assertEqual(queued.get("status"), "Queued")
		job = frappe.get_doc("CRM Export Job", queued["job"])
		self.assertEqual(job.source, "Smart View")
		self.assertEqual(job.reference, self.view)
		self.assertEqual(job.owner, frappe.session.user, "the job must belong to whoever asked")

	def test_a_stranger_cannot_download_a_view_they_cannot_open(self):
		frappe.set_user(STRANGER)
		try:
			with self.assertRaises(frappe.PermissionError):
				smartview.export_view(self.view, "csv")
		finally:
			frappe.set_user("Administrator")
