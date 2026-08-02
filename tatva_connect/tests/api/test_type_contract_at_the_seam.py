# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The declared type is enforced AT THE INGESTION SEAM — for every resource, not one of them.

`_base.cast_declared` is the rule. This module proves the rule is actually REACHED: a lead, a note, a
call, a file and an activity each have exactly one seam a caller's payload passes through, and each of
them resolves through that one rule rather than through five copies of it.

Measured before this was wired (2026-08-02 live proof, item 26): six fields, five different behaviours
for one class of mistake — a Float took "high" and stored 0.0 under a 200, a Date took "not-a-date" and
answered 500 from the database driver, a child section sent as a string leaked orjson's own sentence, a
call duration took a word and stored 0. The type was published for every one of them and nothing held a
caller to it.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.api.test_type_contract_at_the_seam
"""
import datetime
import unittest
from typing import ClassVar
from unittest.mock import patch

import frappe

from tatva_connect.activity import api as activity_brain
from tatva_connect.api import partner, partner_activity, partner_call, partner_file, partner_note
from tatva_connect.api.field_spec import collect

VERTICAL, GROUP = "Goodflip-Care", "Anaya"

# The measured fields, named where a test reads better for naming them.
_LAB = "custom_lab_profile"
_FLOAT_FIELD = "hba1c"


class SeamCase(unittest.TestCase):
	"""A refusal at a seam is a ValidationError that NAMES the field the caller sent."""

	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		cls.mp, cls.is_sysmgr = None, True

	def setUp(self):
		self.sp = f"type_seam_{frappe.generate_hash(length=6)}"
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

	def refusal(self, fieldname, fn, *args, **kwargs):
		with self.assertRaises(frappe.ValidationError) as caught:
			fn(*args, **kwargs)
		frappe.clear_messages()
		frappe.local.message_log = []
		self.assertEqual(
			getattr(caught.exception, "fields", None), [fieldname],
			"a refusal must name the field the caller sent, in error.fields",
		)
		return str(caught.exception)


class TestTheLeadSeam(SeamCase):
	"""`partner._collect` is the lead's ONE ingestion seam — every create, update, bulk record and Desk
	import row passes through it."""

	def collect(self, data):
		_u, _mp, _s, parent_fields, child_allow = partner._caller_fields()
		return partner._collect(data, parent_fields, child_allow, allow_routing=False)

	def test_a_float_that_is_a_word_is_refused_naming_the_field(self):
		# Measured: hba1c="high" -> 200, stored 0.000000000, echoed back as "high".
		message = self.refusal(_FLOAT_FIELD, self.collect, {_LAB: [{_FLOAT_FIELD: "high"}]})
		self.assertIn("high", message)

	def test_a_date_that_is_not_a_date_is_refused_before_the_driver_sees_it(self):
		# Measured: custom_dob="not-a-date" -> MySQLdb error 1292 -> 500 "a reason on our side".
		message = self.refusal("custom_dob", self.collect, {"custom_dob": "not-a-date"})
		self.assertIn("YYYY-MM-DD", message)

	def test_a_child_section_sent_as_a_string_names_the_field_and_the_shape(self):
		# Measured: the partner was handed orjson's own "invalid literal: line 1 column 1 (char 0)".
		message = self.refusal(_LAB, self.collect, {_LAB: "not-an-array"})
		self.assertNotIn("invalid literal", message)

	def test_the_write_path_itself_refuses_it(self):
		"""The seam is on the WRITE path, not merely importable: an upsert refuses and stores nothing."""
		phone = "+919812300301"
		item = {"mobile_no": phone, "first_name": "Type Seam", "custom_vertical": VERTICAL,
		        "custom_group": GROUP, _LAB: [{_FLOAT_FIELD: "high"}]}
		_u, mp, s, pf, ca = partner._caller_fields()
		self.refusal(_FLOAT_FIELD, partner._upsert_one, item, mp, s, pf, ca, [])
		self.assertFalse(
			frappe.db.exists("CRM Lead", {"mobile_no": phone, "custom_vertical": VERTICAL}),
			"a refused record must not be half-written",
		)

	def test_valid_values_still_land_exactly_as_they_did(self):
		"""The DNA guard: what the API accepts today it still accepts, and stores."""
		phone = "+919812300302"
		item = {"mobile_no": phone, "first_name": "Type Seam", "custom_vertical": VERTICAL,
		        "custom_group": GROUP, "custom_dob": "1971-04-06",
		        _LAB: {_FLOAT_FIELD: "6.4"}}  # a single object is still wrapped into one row
		_u, mp, s, pf, ca = partner._caller_fields()
		doc, action = partner._upsert_one(item, mp, s, pf, ca, [])
		self.assertEqual(action, "created")
		self.assertEqual(frappe.utils.getdate(doc.custom_dob), datetime.date(1971, 4, 6))
		self.assertEqual(float(doc.get(_LAB)[0].get(_FLOAT_FIELD)), 6.4)

	def test_an_undeclared_key_is_still_dropped_in_silence(self):
		"""The contract decides WHAT may be sent; the type layer never widens or narrows that."""
		out, _children = self.collect({"not_a_catalog_field": "x", "first_name": "Kept"})
		self.assertNotIn("not_a_catalog_field", out)
		self.assertEqual(out["first_name"], "Kept")


class TestTheFieldSpecSeam(SeamCase):
	"""`field_spec.collect` is the ONE seam notes, calls and files ingest through — so the rule is wired
	there once, not three times."""

	def test_a_note_created_at_that_is_not_a_datetime_is_refused_by_name(self):
		# A target-less spec still publishes a type (Datetime), so it is still held to one. Today the
		# caller gets dateutil's own "Unknown string format", which names nothing.
		lead = _mint_lead("+919812300303")
		self.refusal(
			"created_at", partner_note._create_one,
			{"lead": lead.name, "content": "<p>x</p>", "created_at": "not-a-date"},
			self.mp, self.is_sysmgr,
		)

	def test_a_call_duration_that_is_a_word_is_refused_instead_of_stored_as_zero(self):
		# Measured class: cint("high") is 0, so the call logged a duration nobody sent.
		lead = _mint_lead("+919812300304")
		self.refusal(
			"duration", partner_call._create_one,
			{"lead": lead.name, "direction": "Inbound", "from_number": "9812300077",
			 "to_number": "9999999999", "duration": "high"},
			self.mp, self.is_sysmgr,
		)

	def test_a_file_field_sent_as_a_container_is_refused_by_name(self):
		# Driven at the seam: an attach carries BYTES, and the type rule fires before any of them move.
		self.refusal("filename", collect, partner_file.FILE_FIELDS,
		             {"filename": {"a": 1}}, "File")

	def test_the_specs_public_name_is_what_a_refusal_names(self):
		"""`started_at` lands on the `start_time` column. A caller has never heard of `start_time`, so the
		refusal names what they SENT."""
		self.refusal("started_at", collect, partner_call.CALL_FIELDS,
		             {"started_at": "not-a-date"}, "CRM Call Log")

	def test_valid_values_still_collect_onto_their_columns(self):
		out = collect(partner_call.CALL_FIELDS,
		              {"direction": "Inbound", "duration": "187", "started_at": "2026-01-15 10:30:00"},
		              "CRM Call Log")
		self.assertEqual(int(out["duration"]), 187)
		self.assertEqual(out["start_time"], datetime.datetime(2026, 1, 15, 10, 30))

	def test_a_blank_is_still_not_sent(self):
		out = collect(partner_call.CALL_FIELDS, {"duration": "", "status": "Completed"}, "CRM Call Log")
		self.assertNotIn("duration", out)
		self.assertEqual(out["status"], "Completed")


class TestTheActivitySeam(SeamCase):
	"""An activity's answers are typed by `CRM Task Type Field` — the type `activity_schema` publishes.
	The partner surface holds a caller to that same declaration before the brain is handed anything."""

	_CFG: ClassVar[dict] = {
		"fields": [frappe._dict({"fieldname": "weight", "fieldtype": "Float", "label": "Weight"}),
		           frappe._dict({"fieldname": "seen_on", "fieldtype": "Date", "label": "Seen On"}),
		           frappe._dict({"fieldname": "notes", "fieldtype": "Data", "label": "Notes"})]
	}

	def test_an_answer_that_cannot_be_its_declared_type_is_refused(self):
		with patch.object(activity_brain, "_type_config", return_value=self._CFG):
			self.refusal("weight", partner_activity._declared_values, "any::type", {"weight": "heavy"})

	def test_a_valid_answer_arrives_in_its_declared_type(self):
		with patch.object(activity_brain, "_type_config", return_value=self._CFG):
			out = partner_activity._declared_values(
				"any::type", {"weight": "72.5", "seen_on": "2026-01-15", "notes": "fine"})
		self.assertEqual(out["weight"], 72.5)
		self.assertEqual(out["seen_on"], datetime.date(2026, 1, 15))
		self.assertEqual(out["notes"], "fine")

	def test_an_answer_the_type_does_not_declare_is_left_for_the_brain_to_judge(self):
		with patch.object(activity_brain, "_type_config", return_value=self._CFG):
			out = partner_activity._declared_values("any::type", {"undeclared": {"a": 1}})
		self.assertEqual(out, {"undeclared": {"a": 1}})


def _mint_lead(phone):
	"""One lead through the real core, for a seam test that needs something to hang a record off."""
	_u, mp, s, pf, ca = partner._caller_fields()
	doc, _action = partner._upsert_one(
		{"mobile_no": phone, "first_name": "Type Seam", "custom_vertical": VERTICAL,
		 "custom_group": GROUP}, mp, s, pf, ca, [])
	return doc
