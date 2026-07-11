# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The partner API's identity contract — the four axioms, pinned.

These are the rules the whole surface is derived from. If one of these tests goes red, the API is
speaking a second language again:

  1. The primary key of the underlying table is the anchor. A create returns `name`; every
     subsequent read, update and delete is addressed by it. It is the ONLY address.
  2. Deduplication is OURS, never the caller's. A lead is one per (mobile_no, vertical, group). An
     activity, file or call is CREATED — a re-sent create produces a second record.
  3. `external_id` is a cosmetic label. It is stored and echoed back, never interpreted, and never
     resolves a record. A colliding label is inert.
  4. One envelope. Every write returns the full record; every bulk (read AND write) returns the same
     {total, succeeded, failed} summary; every list returns the same page shape.

The caller here is a TRUSTED System Manager (no mapping) — the grain-scoped partner path has its own
adversarial coverage under tatva_connect/tests/authz (A13), which is the differential/metamorphic
framework and stays the one place scoping is proven. This module pins the CONTRACT, not the gate.
"""
import unittest

import frappe

from tatva_connect.api import partner, partner_activity, partner_call
from tatva_connect.api._base import (
	ACTION_CREATED,
	ACTION_DELETED,
	ACTION_FETCHED,
	ACTION_UPDATED,
	EXTERNAL_ID_FIELD,
	_bulk_read,
	_list_ok,
	_run_bulk,
)

VERTICAL, GROUP = "GoodFlip Care", "Anaya"
BULK_SUMMARY_KEYS = ["failed", "succeeded", "total"]


class TestPartnerContract(unittest.TestCase):
	"""Trusted caller (System Manager, no mapping) — unscoped, so the contract is isolated from the gate."""

	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		cls.mp, cls.is_sysmgr = None, True

	def setUp(self):
		self.sp = f"partner_contract_{frappe.generate_hash(length=6)}"
		frappe.db.savepoint(self.sp)
		self._form = frappe.form_dict
		frappe.local.response = frappe._dict()

	def tearDown(self):
		frappe.form_dict = self._form
		frappe.db.rollback(save_point=self.sp)

	# -- helpers -------------------------------------------------------------

	def _lead(self, phone, external_id=None):
		"""Upsert a lead through the real core and return (doc, action)."""
		frappe.form_dict = frappe._dict({
			"mobile_no": phone, "first_name": "Contract Test",
			"custom_vertical": VERTICAL, "custom_group": GROUP,
		})
		if external_id is not None:
			frappe.form_dict["external_id"] = external_id
		_u, mp, s, pf, ca = partner._caller_fields()
		return partner._upsert_one(frappe.form_dict, mp, s, pf, ca, [])

	def _call(self, lead, external_id=None, **extra):
		data = {"lead": lead, "direction": "Inbound",
		        "from_number": "9812300077", "to_number": "9999999999", **extra}
		if external_id is not None:
			data["external_id"] = external_id
		frappe.form_dict = frappe._dict(data)
		return partner_call._create_one(frappe.form_dict, self.mp, self.is_sysmgr)

	# -- axiom 2: dedup is OURS ---------------------------------------------

	def test_lead_dedups_on_phone_and_grain_not_on_external_id(self):
		"""The SAME phone with a DIFFERENT external_id is the SAME lead. The label never splits a lead."""
		phone = "+919812300081"
		doc1, action1 = self._lead(phone, external_id="PARTNER-A")
		doc2, action2 = self._lead(phone, external_id="PARTNER-B-DIFFERENT")

		self.assertEqual(action1, ACTION_CREATED)
		self.assertEqual(action2, ACTION_UPDATED, "a re-sent phone must UPDATE, never create a second lead")
		self.assertEqual(doc1.name, doc2.name, "dedup must be (mobile_no, vertical, group) — NOT external_id")
		self.assertEqual(
			doc2.get(EXTERNAL_ID_FIELD), "PARTNER-B-DIFFERENT",
			"the label is overwritten by the latest write; it is cosmetic and carries no identity",
		)

	def test_a_differing_external_id_never_splits_a_lead(self):
		"""Two DIFFERENT labels on one phone still resolve to ONE lead — the label is not a key."""
		phone = "+919812300082"
		first, _ = self._lead(phone, external_id="X-1")
		second, _ = self._lead(phone, external_id="X-2")
		self.assertEqual(first.name, second.name)
		self.assertEqual(
			frappe.db.count("CRM Lead", {"mobile_no": phone, "custom_vertical": VERTICAL,
			                             "custom_group": GROUP}),
			1, "exactly ONE lead may exist per (phone, vertical, group)",
		)

	def test_call_create_creates_it_does_not_upsert(self):
		"""A re-sent create with the SAME external_id produces a SECOND call. There is no upsert."""
		lead, _ = self._lead("+919812300083")
		one, a1 = self._call(lead.name, external_id="SAME-LABEL")
		two, a2 = self._call(lead.name, external_id="SAME-LABEL")

		self.assertEqual(a1, ACTION_CREATED)
		self.assertEqual(a2, ACTION_CREATED, "a create CREATES — it never silently updates on a label")
		self.assertNotEqual(
			one["name"], two["name"],
			"a re-sent create must mint a SECOND record; external_id must not deduplicate",
		)
		self.assertEqual(one["external_id"], two["external_id"], "both carry the label they were sent")

	# -- axiom 1 + 3: name is the anchor, external_id is inert ---------------

	def test_a_record_is_addressed_by_name(self):
		"""Create -> name; read, update and delete all address by that name."""
		lead, _ = self._lead("+919812300084")
		created, _ = self._call(lead.name, external_id="ANCHOR")
		name = created["name"]

		fetched = partner_call._read_one(name, self.mp, self.is_sysmgr)
		self.assertEqual(fetched["name"], name)

		frappe.form_dict = frappe._dict({"name": name, "status": "Completed", "duration": 187})
		updated, action = partner_call._update_one(name, frappe.form_dict, self.mp, self.is_sysmgr)
		self.assertEqual(action, ACTION_UPDATED)
		self.assertEqual(updated["name"], name, "an update never re-mints the anchor")
		self.assertEqual(int(updated["duration"]), 187)

		partner_call._delete_one(name, self.mp, self.is_sysmgr)
		self.assertFalse(frappe.db.exists("CRM Call Log", name))

	def test_external_id_is_stored_echoed_and_never_resolves(self):
		"""The label round-trips on every read, and is NOT an address: no endpoint takes it."""
		lead, _ = self._lead("+919812300085")
		created, _ = self._call(lead.name, external_id="ECHO-ME")
		self.assertEqual(created["external_id"], "ECHO-ME")
		self.assertEqual(
			partner_call._read_one(created["name"], self.mp, self.is_sysmgr)["external_id"], "ECHO-ME")

		# The label is NOT an address. _read_one takes a `name`; handed the label, it must not resolve.
		with self.assertRaises(
			frappe.DoesNotExistError,
			msg="external_id must never resolve a record — `name` is the only address",
		):
			partner_call._read_one("ECHO-ME", self.mp, self.is_sysmgr)

	def test_external_id_may_be_omitted_entirely(self):
		"""The label is OPTIONAL on every entity. A create without one succeeds and echoes null."""
		lead, _ = self._lead("+919812300086")          # no external_id
		created, action = self._call(lead.name)        # no external_id
		self.assertEqual(action, ACTION_CREATED)
		self.assertIsNone(created["external_id"])
		self.assertIsNone(lead.get(EXTERNAL_ID_FIELD))

	# -- axiom 4: one envelope ----------------------------------------------

	def test_every_bulk_read_and_write_shares_one_summary_shape(self):
		"""Reads and writes emit the SAME {total, succeeded, failed}. No endpoint invents its own."""
		lead, _ = self._lead("+919812300087")
		one, _ = self._call(lead.name)
		two, _ = self._call(lead.name)

		# READ lane — two real names plus one that does not exist.
		frappe.local.response = frappe._dict()
		_bulk_read([one["name"], two["name"], "DOES-NOT-EXIST"],
		           lambda n: partner_call._read_one(n, self.mp, self.is_sysmgr))
		read = frappe.local.response
		self.assertEqual(sorted(read["summary"]), BULK_SUMMARY_KEYS)
		self.assertEqual(dict(read["summary"]), {"total": 3, "succeeded": 2, "failed": 1})
		self.assertEqual([r["index"] for r in read["results"]], [0, 1, 2], "results are input-ordered")
		self.assertEqual(read["results"][0]["action"], ACTION_FETCHED)
		self.assertEqual(read["results"][2]["error"]["code"], "not_found")
		self.assertNotIn("data", read["results"][2], "a failed row carries an error, never data")

		# WRITE lane — the SAME summary keys.
		frappe.local.response = frappe._dict()

		def delete(i, name):
			partner_call._delete_one(name, self.mp, self.is_sysmgr)
			return {"index": i, "status": "success", "action": ACTION_DELETED, "data": {"name": name}}

		_run_bulk([one["name"], "DOES-NOT-EXIST"], delete)
		write = frappe.local.response
		self.assertEqual(sorted(write["summary"]), BULK_SUMMARY_KEYS,
		                 "the write lane must emit the SAME three keys as the read lane")
		self.assertEqual(dict(write["summary"]), {"total": 2, "succeeded": 1, "failed": 1})

	def test_a_failing_bulk_row_does_not_roll_back_the_others(self):
		"""Partial success: one bad row reports its error, the rest still commit."""
		lead, _ = self._lead("+919812300088")
		good, _ = self._call(lead.name)
		frappe.local.response = frappe._dict()

		def delete(i, name):
			partner_call._delete_one(name, self.mp, self.is_sysmgr)
			return {"index": i, "status": "success", "action": ACTION_DELETED, "data": {"name": name}}

		_run_bulk(["DOES-NOT-EXIST", good["name"]], delete)
		self.assertEqual(dict(frappe.local.response["summary"]), {"total": 2, "succeeded": 1, "failed": 1})
		self.assertFalse(frappe.db.exists("CRM Call Log", good["name"]),
		                 "the good row must still have committed despite the bad one")

	def test_every_list_emits_one_page_shape(self):
		"""total / count / offset / limit / has_more, on every entity, with no exceptions."""
		frappe.local.response = frappe._dict()
		_list_ok("calls", [{"name": "a"}, {"name": "b"}], total=5, offset=0, limit=2)
		data = frappe.local.response["data"]
		self.assertEqual(frappe.local.response["action"], ACTION_FETCHED)
		self.assertEqual(
			sorted(data), ["calls", "count", "has_more", "limit", "offset", "total"],
			"every list emits the same five page keys plus its own collection",
		)
		self.assertTrue(data["has_more"], "offset 0 + 2 of 5 rows means more remain")

		frappe.local.response = frappe._dict()
		_list_ok("calls", [{"name": "e"}], total=5, offset=4, limit=2)
		self.assertFalse(frappe.local.response["data"]["has_more"], "the last page reports has_more false")

	def test_a_write_returns_the_full_record(self):
		"""A create, an update and a get hand back the SAME shape — they can never diverge."""
		lead, _ = self._lead("+919812300089", external_id="SHAPE")
		created, _ = self._call(lead.name, external_id="SHAPE")
		fetched = partner_call._read_one(created["name"], self.mp, self.is_sysmgr)
		self.assertEqual(
			sorted(created), sorted(fetched),
			"the create response and the get response must be the same shape",
		)

	# -- discovery -----------------------------------------------------------

	def test_every_schema_states_identity_and_dedup(self):
		"""Discovery carries the contract, so an integrator never has to infer it."""
		for endpoint, entity in (
			(partner_call.call_schema, "call"),
			(__import__("tatva_connect.api.partner_file", fromlist=["x"]).file_schema, "file"),
		):
			with self.subTest(entity=entity):
				frappe.local.response = frappe._dict()
				frappe.form_dict = frappe._dict()
				endpoint()
				data = frappe.local.response["data"]
				self.assertEqual(data["entity"], entity)
				self.assertEqual(data["identity"]["addressed_by"], "name")
				self.assertIn("does not deduplicate", data["dedup"])
				ext = [f for f in data["fields"] if f["fieldname"] == "external_id"][0]
				self.assertEqual(ext["behavior"], "OPTIONAL")
				self.assertFalse(ext["required"], "external_id is OPTIONAL on every entity, always")

	def test_every_schema_field_declares_required_ness(self):
		"""No field is ambiguous: each carries an AIP-203 behavior and a boolean required."""
		frappe.local.response = frappe._dict()
		frappe.form_dict = frappe._dict()
		partner_call.call_schema()
		fields = frappe.local.response["data"]["fields"]
		self.assertTrue(fields)
		for f in fields:
			with self.subTest(field=f["fieldname"]):
				self.assertIn(f["behavior"], ("REQUIRED", "OPTIONAL", "OUTPUT_ONLY"))
				self.assertIsInstance(f["required"], bool)
		direction = [f for f in fields if f["fieldname"] == "direction"][0]
		self.assertTrue(direction["required"], "direction is the call's one required field")
		self.assertEqual(direction["allowed_values"], ["Inbound", "Outbound"])
