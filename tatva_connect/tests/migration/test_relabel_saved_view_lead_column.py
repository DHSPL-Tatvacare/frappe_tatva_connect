# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A4 — the rename reaches the reps who already customised, and reaches nothing else.

A rep who ever changed a column or a sort has a `CRM View Settings` row, and the server returns that
row's columns instead of the declaration's. So the label edit in `list_engine/columns.py` is invisible to
exactly the people most likely to notice it, and the patch is the repair.

The whole risk of a repair like this is that it rewrites more than it was asked to. So the four stories
here are: it changes the label it was pointed at; it leaves everything ELSE in the same row alone,
including the width the rep dragged and a column they added; it does not touch a row that is not standard
or not one of the three doctypes; and running it twice changes nothing the second time.

It calls `execute()` directly. Proving the PATCH — that it is registered, ordered and replays against an
already-migrated database twice — is the integration turn's job (plan §9), not this module's.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.migration.test_relabel_saved_view_lead_column
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.patches import relabel_saved_view_lead_column as patch

VIEW_SETTINGS = "CRM View Settings"
PROBE = "ZZ Relabel Probe"

# What a rep's row looks like before the repair: the old label, a width they dragged, and a column of
# their own. Only the first line of the first dict may change.
OLD_COLUMNS = [
	{"label": "Task ID", "type": "Data", "key": "name", "width": "10rem"},
	{
		"label": "Lead ID",
		"type": "Dynamic Link",
		"key": "reference_docname",
		"options": "reference_doctype",
		"width": "22rem",
	},
	{"label": "Priority", "type": "Select", "key": "priority", "width": "8rem"},
]


class TestRelabelSavedViewLeadColumn(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self._wipe()

	def tearDown(self):
		self._wipe()

	def _wipe(self):
		# The patch commits, as a patch must, so the rollback FrappeTestCase gives cannot take the probe
		# rows back out; this deletes them for real or the site keeps them.
		frappe.db.delete(VIEW_SETTINGS, {"label": ["like", f"{PROBE}%"]})
		frappe.db.commit()

	def _seed(self, dt, label, is_standard=1, columns=None):
		doc = frappe.get_doc(
			{
				"doctype": VIEW_SETTINGS,
				"dt": dt,
				"label": label,
				"type": "list",
				"is_standard": is_standard,
				"user": "Administrator",
				"columns": frappe.as_json(columns if columns is not None else OLD_COLUMNS),
				"rows": frappe.as_json(["name", "reference_docname", "priority"]),
			}
		).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input
		return doc.name

	def _columns(self, name):
		return frappe.parse_json(frappe.db.get_value(VIEW_SETTINGS, name, "columns"))

	def test_the_lead_column_is_relabelled_on_all_three_doctypes(self):
		names = {
			dt: self._seed(dt, f"{PROBE} {dt}")
			for dt in ("CRM Task", "CRM Call Log", "FCRM Note")
		}
		patch.execute()
		for dt, name in names.items():
			with self.subTest(doctype=dt):
				lead = [c for c in self._columns(name) if c["key"] == "reference_docname"][0]
				self.assertEqual(lead["label"], "Lead")

	def test_nothing_else_in_the_row_moves(self):
		name = self._seed("CRM Task", f"{PROBE} intact")
		patch.execute()
		after = self._columns(name)
		expected = [dict(c) for c in OLD_COLUMNS]
		expected[1]["label"] = "Lead"
		self.assertEqual(after, expected)

	def test_a_row_that_is_not_ours_is_left_alone(self):
		"""Not standard, or not a child listing doctype: neither is a row this repair was asked about."""
		private = self._seed("CRM Task", f"{PROBE} private", is_standard=0)
		other = self._seed("CRM Deal", f"{PROBE} deal")
		patch.execute()
		for name in (private, other):
			with self.subTest(view=name):
				lead = [c for c in self._columns(name) if c["key"] == "reference_docname"][0]
				self.assertEqual(lead["label"], "Lead ID")

	def test_a_second_run_changes_nothing(self):
		name = self._seed("CRM Task", f"{PROBE} twice")
		patch.execute()
		after_first = self._columns(name)
		modified_first = frappe.db.get_value(VIEW_SETTINGS, name, "modified")
		patch.execute()
		self.assertEqual(self._columns(name), after_first)
		self.assertEqual(frappe.db.get_value(VIEW_SETTINGS, name, "modified"), modified_first)

	def test_a_site_with_no_such_row_is_a_no_op(self):
		patch.execute()
		self.assertEqual(
			frappe.get_all(VIEW_SETTINGS, filters={"label": ["like", f"{PROBE}%"]}), []
		)
