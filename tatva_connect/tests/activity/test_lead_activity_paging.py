# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""`lead_activity` pages the lead's card tabs the way the Leads LIST page pages — and nothing else.

Step 3 of docs/plans/2026-07-27-lead-detail-server-paging-leads-pattern.md. The lead detail used to ship
its whole history in one call — measured at 264 KB and 459 rendered rows on the fattest dev lead — because
the tabs sliced an already-loaded list on the client. They now ask the server for a page.

The pattern is the Leads list page's, copied rather than adapted, so what this module locks is the COPY:

  * the envelope is `crm/api/doc.py:533`'s exactly — {data, page_length, page_length_count, total_count,
    row_count}. The frontend binds `ListFooter` straight to `row_count`/`total_count`, so a renamed or
    missing key is a silently empty footer, not an error;
  * Load More is a REFETCH with a bigger `page_length` (ViewControls.vue:1058), never a cursor. A page of
    40 is therefore the first 40, not rows 21-40;
  * `total_count` counts the whole filtered set, not the page — it is the "of 103" a rep reads;
  * `order_by` is an ALLOWLIST. A caller's string never reaches SQL, so an unknown field falls back to
    the default instead of ordering by, or executing, whatever was sent.

It also locks the thing a paging change is most likely to break quietly: a paged row must carry the same
derived fields the whole-list payload carried, or a card that reads `_duration`, `attachments` or `due`
renders blank and no test notices. `test_page_rows_are_decorated_like_the_old_payload` is that check.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.activity.test_lead_activity_paging
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.api.activities import lead_activity

ENVELOPE_KEYS = {"data", "page_length", "page_length_count", "total_count", "row_count"}


class TestLeadActivityPaging(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.lead = frappe.get_doc(
			{"doctype": "CRM Lead", "first_name": "ZZ Paging Probe", "mobile_no": "+919000000042"}
		).insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture, runs as Administrator
		# 25 calls so a default page (20) leaves a second page to ask for, which is the whole contract.
		for i in range(25):
			frappe.get_doc(
				{
					"doctype": "CRM Call Log",
					"id": f"zz-paging-{i}",
					"telephony_medium": "Manual",
					"type": "Outgoing",
					"status": "Completed",
					"duration": 30 + i,
					"from": "+919000000042",
					"to": "+919000000043",
					"reference_doctype": "CRM Lead",
					"reference_docname": cls.lead.name,
				}
			).insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture, runs as Administrator
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.db.delete("CRM Call Log", {"reference_docname": cls.lead.name})
		frappe.db.delete("CRM Timeline Event", {"reference_name": cls.lead.name})
		frappe.delete_doc("CRM Lead", cls.lead.name, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	def test_envelope_is_the_leads_list_envelope(self):
		"""The frontend binds ListFooter to these exact keys; a rename empties the footer silently."""
		page = lead_activity(self.lead.name, "call")
		self.assertEqual(set(page.keys()), ENVELOPE_KEYS)
		self.assertEqual(page["page_length"], 20)
		self.assertEqual(page["page_length_count"], 20)

	def test_first_page_is_bounded_and_total_counts_everything(self):
		page = lead_activity(self.lead.name, "call")
		self.assertEqual(page["row_count"], 20)
		self.assertEqual(len(page["data"]), 20)
		self.assertEqual(page["total_count"], 25)

	def test_load_more_refetches_a_bigger_page_rather_than_the_next_slice(self):
		"""ViewControls.vue:1058 grows page_length and reloads. So 40 rows means the FIRST 40 — a
		cursor would return rows 21-40 and the rail would lose its top."""
		first = lead_activity(self.lead.name, "call")
		more = lead_activity(self.lead.name, "call", page_length=40)
		self.assertEqual(more["row_count"], 25)
		self.assertEqual(
			[r["name"] for r in more["data"][:20]], [r["name"] for r in first["data"]]
		)

	def test_order_by_is_an_allowlist_not_a_passthrough(self):
		default = lead_activity(self.lead.name, "call")
		injected = lead_activity(self.lead.name, "call", order_by="name; DROP TABLE x")
		unknown = lead_activity(self.lead.name, "call", order_by="priority desc")
		self.assertEqual(
			[r["name"] for r in injected["data"]], [r["name"] for r in default["data"]]
		)
		self.assertEqual([r["name"] for r in unknown["data"]], [r["name"] for r in default["data"]])

	def test_sort_direction_is_honoured(self):
		asc = lead_activity(self.lead.name, "call", order_by="creation asc")
		desc = lead_activity(self.lead.name, "call", order_by="creation desc")
		self.assertNotEqual(asc["data"][0]["name"], desc["data"][0]["name"])
		self.assertLessEqual(str(asc["data"][0]["creation"]), str(asc["data"][-1]["creation"]))
		self.assertGreaterEqual(str(desc["data"][0]["creation"]), str(desc["data"][-1]["creation"]))

	def test_filters_narrow_both_the_page_and_the_total(self):
		"""A filter that only narrowed `data` would leave the footer reading "20 of 25" forever."""
		page = lead_activity(
			self.lead.name, "call", filters=frappe.as_json({"status": "No Answer"})
		)
		self.assertEqual(page["row_count"], 0)
		self.assertEqual(page["total_count"], 0)

	def test_page_rows_are_decorated_like_the_old_payload(self):
		"""The quiet break: a paged call row without `_duration`/`_caller` renders a blank card, and
		nothing else would notice. parse_call_log must still run on the page."""
		row = lead_activity(self.lead.name, "call")["data"][0]
		self.assertIn("_duration", row)
		self.assertIn("_caller", row)
		self.assertIn("_receiver", row)

	def test_an_unknown_kind_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			lead_activity(self.lead.name, "sausages")

	def test_visibility_is_decided_by_the_lead(self):
		"""A sub-row is visible iff its lead is — the native timeline's rule (crm/api/activities.py:176),
		not a permission on CRM Call Log."""
		with self.assertRaises(frappe.PermissionError):
			original = frappe.has_permission
			frappe.has_permission = lambda *a, **k: False
			try:
				lead_activity(self.lead.name, "call")
			finally:
				frappe.has_permission = original
