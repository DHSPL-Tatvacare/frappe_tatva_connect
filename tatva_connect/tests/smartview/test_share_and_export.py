# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""SHARING AND EXPORT — both on frappe's own seams, and neither one a way past the permission model.

THE ONE IDEA BEHIND BOTH. A Smart View is a saved QUESTION, never a saved answer. Sharing one grants no
data: every run still ANDs the VIEWER's own permission conditions, so two people opening one shared view
see different rows. Exporting one re-runs the very same composer, so a download can never contain what
the list would not have shown.

WHAT IS FRAPPE'S AND NOT OURS:
  * who a view is shared with        -> `frappe.share` (DocShare). No share table of our own.
  * may this caller download         -> the native EXPORT permission on the driving doctype, an ordinary
                                        role permission an operator ticks.
  * what left the building           -> `Access Log`, frappe's own download audit.
  * the file itself                  -> `tabular.py`, which is already the one csv/xlsx door.

The negative tests are the point: a share must not widen rows, and an export must not widen either rows
or columns.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.smartview.test_share_and_export
"""
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

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


class TestSharingIsDocShare(_ShareCase):
	"""No share table of our own — the framework's, or it would drift from every other share in the app."""

	def test_a_private_view_reaches_only_its_owner(self):
		self.assertIn(self.view, self._tabs_for(OWNER))
		self.assertNotIn(self.view, self._tabs_for(STRANGER))

	def test_sharing_puts_it_on_the_other_persons_tabs(self):
		frappe.set_user("Administrator")
		smartview.share_view(self.view, FRIEND)
		self.assertIn(self.view, self._tabs_for(FRIEND))
		self.assertNotIn(self.view, self._tabs_for(STRANGER), "a share reached someone it was not given to")

	def test_it_really_is_a_docshare_row(self):
		"""If this ever stops being a DocShare, sharing has grown a second mechanism."""
		frappe.set_user("Administrator")
		smartview.share_view(self.view, FRIEND)
		self.assertTrue(frappe.db.exists("DocShare", {
			"share_doctype": "CRM Smart View", "share_name": self.view, "user": FRIEND,
		}))
		self.assertIn(self.view, frappe.share.get_shared("CRM Smart View", FRIEND))

	def test_unsharing_takes_it_back(self):
		frappe.set_user("Administrator")
		smartview.share_view(self.view, FRIEND)
		smartview.unshare_view(self.view, FRIEND)
		self.assertNotIn(self.view, self._tabs_for(FRIEND))

	def test_a_shared_view_can_be_opened(self):
		frappe.set_user("Administrator")
		smartview.share_view(self.view, FRIEND)
		frappe.set_user(FRIEND)
		try:
			self.assertEqual(smartview.get_view(self.view)["name"], self.view)
		finally:
			frappe.set_user("Administrator")

	def test_a_stranger_still_cannot_open_it(self):
		frappe.set_user(STRANGER)
		try:
			with self.assertRaises(frappe.PermissionError):
				smartview.get_view(self.view)
		finally:
			frappe.set_user("Administrator")

	def test_only_someone_who_may_edit_the_view_may_share_it(self):
		"""The same gate as every other write here — a reader cannot hand the view on."""
		frappe.set_user("Administrator")
		smartview.share_view(self.view, FRIEND)
		frappe.set_user(FRIEND)
		try:
			with self.assertRaises(frappe.PermissionError):
				smartview.share_view(self.view, STRANGER)
		finally:
			frappe.set_user("Administrator")
		self.assertNotIn(self.view, self._tabs_for(STRANGER))

	def test_making_it_public_is_operator_only_and_disowns_it(self):
		"""crm's own `public()` rule: a public view belongs to nobody, so its owner is cleared."""
		frappe.set_user(OWNER)
		try:
			with self.assertRaises(frappe.PermissionError):
				smartview.set_public(self.view, 1)
		finally:
			frappe.set_user("Administrator")
		smartview.set_public(self.view, 1)
		row = frappe.db.get_value("CRM Smart View", self.view, ["is_standard", "owner_user"], as_dict=True)
		self.assertEqual(row.is_standard, 1)
		self.assertFalse(row.owner_user)
		self.assertIn(self.view, self._tabs_for(STRANGER), "a public view did not reach everyone")


class TestExportIsTheScreenAsAFile(_ShareCase):
	"""An export re-runs the composer. It cannot widen rows, and it cannot widen columns."""

	def test_it_needs_the_native_export_permission(self):
		"""Not a permission invented here — the ordinary role flag an operator ticks on the doctype."""
		with patch("frappe.has_permission", return_value=False):
			with self.assertRaises(frappe.PermissionError):
				smartview.export_view(self.view, "csv")

	def test_the_button_is_offered_on_the_same_answer_it_enforces(self):
		with patch("frappe.has_permission", return_value=False):
			self.assertFalse(smartview.can_export("Lead"))
		with patch("frappe.has_permission", return_value=True):
			self.assertTrue(smartview.can_export("Lead"))

	def test_it_re_runs_get_data_rather_than_querying_again(self):
		"""THE property that keeps an export honest: one composer, so permissions and columns can never
		drift between what is shown and what is downloaded."""
		with patch.object(smartview, "get_data", wraps=smartview.get_data) as composer, \
		     patch.object(smartview.tabular, "respond") as respond:
			smartview.export_view(self.view, "csv")
		# Called at least once, and ALWAYS for this view — not "exactly once", because a view larger than
		# one page is walked page by page. What matters is that every row came through the composer.
		self.assertTrue(composer.called)
		self.assertTrue(all(c.args[0] == self.view for c in composer.call_args_list))
		respond.assert_called_once()

	def test_the_file_carries_the_views_own_columns(self):
		with patch.object(smartview.tabular, "respond") as respond:
			smartview.export_view(self.view, "csv")
		header = respond.call_args.args[0]
		labels = [c["label"] for c in smartview.get_data(self.view, page_size=1)["columns"]]
		self.assertEqual(header, labels, "the download's header is not the view's own columns")

	def test_a_filter_narrows_the_download_too(self):
		"""The export takes the SAME filters the screen has, so a filtered list downloads filtered."""
		cat = {c["field_key"]: c for c in smartview.field_catalog("Lead")}
		key = next(k for k, c in cat.items() if c["filterable"] and c["fieldtype"] == "Data")
		with patch.object(smartview.tabular, "respond") as respond:
			smartview.export_view(self.view, "csv",
			                      filters=frappe.as_json([[key, "=", "ZZ nothing matches this"]]))
		self.assertEqual(respond.call_args.args[1], [], "a filter that matches nothing still exported rows")

	def test_it_goes_through_the_one_file_door(self):
		"""`tabular.py` writes every csv and xlsx in this app; a second writer here would be a second rule
		about what a file is."""
		with patch.object(smartview.tabular, "respond") as respond:
			smartview.export_view(self.view, "xlsx")
		self.assertEqual(respond.call_args.args[2], "xlsx")

	def test_it_exports_every_row_not_just_the_first_page(self):
		"""THE BUG THIS LOCKS. `get_data` caps a page at PAGE_MAX, so asking it for 5,000 rows returned
		200 and the download looked complete — 667 of 867 rows silently missing. An export that quietly
		drops rows is worse than one that refuses, because nobody can see that it did."""
		total = smartview.get_data(self.view, page_size=1)["total"]
		if total <= smartview.PAGE_MAX:
			self.skipTest(f"only {total} rows on this bench — a single page cannot prove paging")
		with patch.object(smartview.tabular, "respond") as respond:
			smartview.export_view(self.view, "csv")
		exported = len(respond.call_args.args[1])
		self.assertGreater(exported, smartview.PAGE_MAX,
		                   f"the export stopped at one page ({exported} rows) of {total}")
		self.assertEqual(exported, min(total, smartview.EXPORT_MAX_ROWS))

	def test_an_unknown_format_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			smartview.export_view(self.view, "pdf")

	def test_every_download_is_logged(self):
		"""An export is the one read that leaves the building, so it lands in frappe's own Access Log."""
		before = frappe.db.count("Access Log")
		with patch.object(smartview.tabular, "respond"):
			smartview.export_view(self.view, "csv")
		self.assertGreater(frappe.db.count("Access Log"), before, "the download left no audit trail")

	def test_a_stranger_cannot_download_a_view_they_cannot_open(self):
		frappe.set_user(STRANGER)
		try:
			with self.assertRaises(frappe.PermissionError):
				smartview.export_view(self.view, "csv")
		finally:
			frappe.set_user("Administrator")
