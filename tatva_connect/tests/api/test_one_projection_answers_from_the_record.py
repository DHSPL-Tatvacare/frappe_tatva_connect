# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A write and a read are ONE answer, and it is the STORED one.

`_curate` calls itself "the ONE lead projection: every read AND every write returns this, so a create,
an update and a get can never hand back different shapes". It kept that promise about the SHAPE and
broke it about the VALUES, because of what it was handed: on a read the doc came from the database, on
a create it was the in-memory object just inserted. Measured 2026-08-02 (item 6a): the same lead read
`"hba1c": null` from lead_create and `0.0` from lead_get, and a partner sending `"high"` was handed
`"high"` straight back while the row held `0.0` — the write endpoint confirming a value that was never
stored.

The second rule here is the other half of the same projection: a field that is OUTPUT_ONLY is READ-ONLY,
never INVISIBLE. `custom_substage` (stage) joined `lead_owner` in RESERVED_FIELDS, which routes it out of
the writable catalog and into `audit` — and no lead endpoint projected `audit`, so a field partners read
today vanished from every response. `lead_schema` has advertised `owner`, `creation`, `modified` and
`lead_owner` as OUTPUT_ONLY for as long as it has existed and no response has ever carried one of them.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.api.test_one_projection_answers_from_the_record
"""
import unittest
from unittest.mock import patch

import frappe

from tatva_connect.api import partner

VERTICAL, GROUP = "Goodflip-Care", "Anaya"

_LAB = "custom_lab_profile"
_LAB_KEY = "report_date"
# The domain field that is still OUTPUT_ONLY. Stage used to stand here; it is now an ordinary
# catalog field a contract may tick, so the rule is pinned on the one field that still holds it.
_OUTPUT_ONLY = "lead_owner"


class ProjectionCase(unittest.TestCase):
	"""Drives the real endpoints, so the assertion is about what a partner receives on the wire."""

	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")

	def setUp(self):
		self.sp = f"one_projection_{frappe.generate_hash(length=6)}"
		frappe.db.savepoint(self.sp)
		self._form = frappe.form_dict
		frappe.local.response = frappe._dict()

	def tearDown(self):
		frappe.form_dict = self._form
		frappe.clear_messages()
		frappe.local.message_log = []
		try:
			frappe.db.rollback(save_point=self.sp)
		except Exception:
			frappe.db.rollback()

	def create(self, **fields):
		frappe.local.response = frappe._dict()
		frappe.form_dict = frappe._dict({
			"first_name": "Projection Test", "custom_vertical": VERTICAL, "custom_group": GROUP,
			**fields,
		})
		partner.lead_create()
		return frappe.local.response["data"]

	def read(self, name):
		frappe.local.response = frappe._dict()
		frappe.form_dict = frappe._dict({"name": name})
		partner.lead_get()
		return frappe.local.response["data"]


class TestTheWriteAnswersFromTheStoredRecord(ProjectionCase):

	def _agree(self, wrote, read, sent, where):
		"""Both endpoints must describe every field the caller SENT identically.

		Only those. A field the caller left out reads None from the doc just written and 0.0 once the
		NOT NULL DEFAULT 0 column holds it — the column's accepted behaviour, and not worth a second
		full read of the record (parent plus every child table) on every single write to paper over."""
		for field in sent:
			self.assertEqual(wrote.get(field), read.get(field),
			                 f"{where}: `{field}` was sent, so a write and a read must agree on it")

	def test_create_and_get_hand_back_the_same_answer(self):
		"""THE defect, end to end: one record, two endpoints, one answer for every field sent."""
		sent = {"mobile_no": "+919812300401", "first_name": "Projection Test"}
		created = self.create(**{**sent, _LAB: [{_LAB_KEY: "2026-01-15", "hba1c": "6.4"}]})
		fetched = self.read(created["name"])
		self._agree(created, fetched, sent, "lead_create vs lead_get")
		self._agree(created[_LAB][0], fetched[_LAB][0], {_LAB_KEY, "hba1c"}, "the lab row")

	def test_update_and_get_hand_back_the_same_answer(self):
		created = self.create(mobile_no="+919812300402")
		sent = {"last_name": "Updated"}
		frappe.local.response = frappe._dict()
		frappe.form_dict = frappe._dict({"name": created["name"], **sent,
		                                 _LAB: [{_LAB_KEY: "2026-01-16"}]})
		partner.lead_update()
		updated = frappe.local.response["data"]
		self._agree(updated, self.read(created["name"]), sent, "lead_update vs lead_get")

	def test_a_value_the_column_normalises_is_echoed_as_the_column_holds_it(self):
		"""The response is the record, not the request: a stored Float reads back as a number."""
		created = self.create(mobile_no="+919812300403",
		                      **{_LAB: [{_LAB_KEY: "2026-01-17", "hba1c": "6.4"}]})
		row = created[_LAB][0]
		self.assertEqual(float(row["hba1c"]), 6.4)
		self.assertEqual(row["hba1c"], self.read(created["name"])[_LAB][0]["hba1c"])

	def test_the_callers_own_label_still_comes_straight_back(self):
		"""The DNA guard on the re-read: external_id is stamped after the insert, so a projection that
		reads the record must still carry it."""
		created = self.create(mobile_no="+919812300404", external_id="PARTNER-LABEL-1")
		self.assertEqual(created["external_id"], "PARTNER-LABEL-1")
		self.assertEqual(created["external_id"], self.read(created["name"])["external_id"])

	def test_the_envelope_is_unchanged(self):
		"""The published DNA: same keys, same routing echo, same address."""
		created = self.create(mobile_no="+919812300405")
		for key in ("name", "external_id", "source", "custom_vertical", "custom_group",
		            "custom_current_program", "mobile_no"):
			self.assertIn(key, created, key)
		self.assertEqual(created["custom_vertical"], VERTICAL)
		self.assertEqual(created["mobile_no"], "+919812300405")


class TestOutputOnlyIsReadableNotInvisible(ProjectionCase):

	def test_every_audit_field_is_projected(self):
		"""`lead_schema` advertises each of these as OUTPUT_ONLY. A schema that names a field no response
		carries is a lie, whichever way round it is."""
		created = self.create(mobile_no="+919812300406")
		audit = {a["fieldname"] for a in partner._build_catalog()["audit"]}
		self.assertTrue(audit, "no audit fields catalogued — this bench cannot prove the rule")
		self.assertEqual(
			audit - set(created), set(),
			"a field lead_schema advertises OUTPUT_ONLY is carried by no response",
		)

	def test_a_stored_output_only_field_is_readable(self):
		"""The regression: making a field unwritable must not have made it unreadable."""
		if _OUTPUT_ONLY not in {a["fieldname"] for a in partner._build_catalog()["audit"]}:
			self.skipTest(f"{_OUTPUT_ONLY} is not catalogued on this bench")
		created = self.create(mobile_no="+919812300407")
		# Written the way the assignment rule's own save would leave it, bypassing the API entirely.
		frappe.db.set_value("CRM Lead", created["name"], _OUTPUT_ONLY, "zz-fixture-owner@example.test",
		                    update_modified=False)
		self.assertEqual(self.read(created["name"])[_OUTPUT_ONLY], "zz-fixture-owner@example.test")

	def test_readable_never_means_writable(self):
		"""The other half: a partner sending an OUTPUT_ONLY field is ignored, not obeyed."""
		created = self.create(mobile_no="+919812300408",
		                      **{_OUTPUT_ONLY: "zz-partner-sent@example.test"})
		self.assertNotEqual(
			frappe.db.get_value("CRM Lead", created["name"], _OUTPUT_ONLY),
			"zz-partner-sent@example.test",
			"an OUTPUT_ONLY field must stay unwritable however it is projected",
		)

	def test_a_duplicated_catalog_row_projects_one_key(self):
		"""A duplicate `CRM Lead API Field` row for one fieldname is operator data this code may not
		defend against — but it must not double a key or crash the projection either."""
		created = self.create(mobile_no="+919812300409")
		straight = self.read(created["name"])
		cat = dict(partner._build_catalog())
		cat["audit"] = [*cat["audit"], *cat["audit"]]
		with patch.object(partner, "_catalog", return_value=cat):
			doubled = self.read(created["name"])
		self.assertEqual(set(doubled), set(straight))
		self.assertEqual(doubled, straight)


if __name__ == "__main__":
	unittest.main()
