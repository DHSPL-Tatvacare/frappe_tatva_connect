# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The API may only claim what it can actually support — the type, the code, and the status.

Four claims a live proof run caught the partner API making that it could not stand behind:

  1. Every schema PUBLISHES a `type` per field and nothing held a caller to it. Six fields, five
     different behaviours for one class of mistake: a Float took "high" and stored 0.0, a Date took
     "not-a-date" and answered 500 from the database driver, a child section sent as a string leaked
     orjson's own sentence. Where it appeared to work it was incidental — three unrelated mechanisms
     each catching their own case.
  2. A mapped exception carrying NO message was returned to the partner under a SPECIFIC code and
     never logged: 1,225 async bulk failures were told `forbidden` — pointing at the one thing that
     was fine, their credentials — while the real cause was an internal queue overflow, and the
     Error Log held zero rows for the lot.
  3. Frappe's own `ValidationError.http_status_code` is 417, so a request the FRAMEWORK refused
     before dispatch came back as `server_error`. A plain GET carrying `Content-Type:
     application/json` — which most HTTP clients set by default — hit it on a perfectly valid read.
  4. Stage is set by reps in the CRM, never by a partner, and nothing said so.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.api.test_declared_contract_enforced
"""
import datetime
import unittest
from unittest.mock import patch

import frappe
from frappe.model import data_fieldtypes, table_fields

from tatva_connect.api import _base


class TypeContractCase(unittest.TestCase):
	"""Shared assertion: a refusal must be a ValidationError that NAMES the field."""

	def refusal(self, doctype, fieldname, value, **kw):
		with self.assertRaises(frappe.ValidationError) as caught:
			_base.cast_declared(doctype, fieldname, value, **kw)
		frappe.clear_messages()
		frappe.local.message_log = []
		self.assertEqual(getattr(caught.exception, "fields", None), [fieldname])
		return str(caught.exception)


class TestDeclaredTypeIsEnforced(TypeContractCase):
	"""A value that cannot be a value of its DECLARED type is refused, and the field is named."""

	def test_float_refuses_a_word_instead_of_storing_zero(self):
		# The measured defect: hba1c="high" -> HTTP 200, stored 0.000000000, echoed back as "high".
		message = self.refusal("CRM Lab Profile", "hba1c", "high")
		self.assertIn("hba1c", message)
		self.assertIn("Float", message)
		self.assertIn("high", message)

	def test_date_refuses_a_non_date_instead_of_reaching_the_driver(self):
		# The measured defect: custom_dob="not-a-date" -> MySQLdb.OperationalError 1292, unmapped,
		# answered 500 "a reason on our side" and burned an Error Log row per occurrence.
		message = self.refusal("CRM Lead", "custom_dob", "not-a-date")
		self.assertIn("custom_dob", message)
		self.assertIn("YYYY-MM-DD", message)

	def test_the_mysql_zero_date_is_not_a_date(self):
		self.refusal("CRM Lead", "custom_dob", "0000-00-00")

	def test_datetime_refuses_a_non_datetime(self):
		self.refusal("CRM Lead", "created_at", "nope", fieldtype="Datetime")

	def test_time_refuses_a_non_time(self):
		# get_timedelta("nope") is timedelta(0) — the same silent-default disease flt has.
		self.refusal("CRM Lead", "some_time", "nope", fieldtype="Time")

	def test_int_refuses_a_word(self):
		self.refusal("CRM Lead", "some_count", "many", fieldtype="Int")

	def test_check_refuses_a_word_outside_the_boolean_vocabulary(self):
		# cint(sbool("yes")) is 0 — "yes" silently became false.
		self.refusal("CRM Lead", "some_flag", "yes", fieldtype="Check")

	def test_check_refuses_a_number_that_is_not_a_boolean(self):
		self.refusal("CRM Lead", "some_flag", 7, fieldtype="Check")

	def test_text_refuses_a_container(self):
		# cstr({"a": 1}) is a Python repr — a JSON object is not a Data value.
		self.refusal("CRM Lead", "custom_city", {"a": 1})

	def test_a_child_section_sent_as_a_string_names_the_field_and_the_shape(self):
		# The measured defect: the partner was handed orjson's own text,
		# "invalid literal: line 1 column 1 (char 0)" — no field, no expected shape.
		message = self.refusal("CRM Lead", "custom_lab_profile", "not-an-array")
		self.assertNotIn("invalid literal", message)
		self.assertIn("custom_lab_profile", message)
		self.assertIn("array", message.lower())

	def test_a_child_section_sent_as_a_number_is_not_a_row(self):
		self.refusal("CRM Lead", "custom_lab_profile", 5)


class TestValidValuesAreUntouched(unittest.TestCase):
	"""The DNA guard: everything the API accepts today it still accepts, in the declared type."""

	def cast(self, doctype, fieldname, value, **kw):
		return _base.cast_declared(doctype, fieldname, value, **kw)

	def test_an_iso_date_becomes_a_date(self):
		self.assertEqual(self.cast("CRM Lead", "custom_dob", "1971-04-06"), datetime.date(1971, 4, 6))

	def test_a_float_string_with_commas_still_parses(self):
		# frappe's flt strips commas; this contract must not be narrower than what already works.
		self.assertEqual(self.cast("CRM Lab Profile", "hba1c", "1,234.5"), 1234.5)

	def test_a_json_number_is_a_float(self):
		self.assertEqual(self.cast("CRM Lab Profile", "hba1c", 6.4), 6.4)

	def test_a_link_value_is_handed_on_exactly_as_sent(self):
		# A grain-scoped composite PK is resolved downstream by taxonomy.picklist, which must see
		# the human value the caller sent, byte for byte.
		value = "Niva-Bupa::RNR after 3 attempts"
		self.assertEqual(self.cast("CRM Lead", "custom_substage", value), value)

	def test_a_select_value_is_handed_on_for_frappe_to_judge(self):
		self.assertEqual(self.cast("CRM Lead", "custom_gender", "Male"), "Male")

	def test_a_single_child_object_is_still_wrapped_into_an_array(self):
		# The existing convenience in partner._collect: one object means one row.
		self.assertEqual(self.cast("CRM Lead", "custom_lab_profile", {"hba1c": 6.4}), [{"hba1c": 6.4}])

	def test_a_child_array_sent_as_a_json_string_still_parses(self):
		self.assertEqual(self.cast("CRM Lead", "custom_lab_profile", '[{"hba1c": 6.4}]'),
		                 [{"hba1c": 6.4}])

	def test_a_blank_is_never_a_type_error(self):
		# "An empty string is 'not sent', never 'erase this'" — a blank must not become a refusal.
		for blank in (None, ""):
			self.assertEqual(self.cast("CRM Lead", "custom_dob", blank), blank)

	def test_check_reads_frappes_own_boolean_vocabulary(self):
		for sent, stored in (("true", 1), ("false", 0), ("1", 1), ("0", 0), (True, 1), (0, 0)):
			self.assertEqual(self.cast("CRM Lead", "f", sent, fieldtype="Check"), stored, sent)


class TestTheRowDoorIsTheOneCallSitesMake(TypeContractCase):
	"""`cast_declared_row` is what an ingestion seam calls: one payload in, one held to its types out."""

	def test_a_row_is_cast_field_by_field(self):
		out = _base.cast_declared_row("CRM Lab Profile", {"hba1c": "6.4", "report_date": "2025-12-13"})
		self.assertEqual(out["hba1c"], 6.4)
		self.assertEqual(out["report_date"], datetime.date(2025, 12, 13))

	def test_a_bad_value_anywhere_in_the_row_refuses_naming_that_field(self):
		with self.assertRaises(frappe.ValidationError) as caught:
			_base.cast_declared_row("CRM Lab Profile", {"hba1c": "high", "report_date": "2025-12-13"})
		frappe.clear_messages()
		frappe.local.message_log = []
		self.assertEqual(caught.exception.fields, ["hba1c"])

	def test_a_key_the_doctype_does_not_declare_passes_through(self):
		# `_delete` is the upsert engine's own flag, and an undeclared key is the contract's business
		# (is_writable, the field grid) — not this layer's.
		out = _base.cast_declared_row("CRM Lab Profile", {"_delete": True, "not_a_column": "x"})
		self.assertEqual(out, {"_delete": True, "not_a_column": "x"})

	def test_the_callers_dict_is_not_mutated(self):
		sent = {"hba1c": "6.4"}
		_base.cast_declared_row("CRM Lab Profile", sent)
		self.assertEqual(sent, {"hba1c": "6.4"})

	def test_declared_types_override_the_meta_lookup(self):
		# A FieldSpec with target=None (partner_note's `created_at`) has no column to type it.
		with self.assertRaises(frappe.ValidationError):
			_base.cast_declared_row("CRM Note", {"created_at": "nope"}, types={"created_at": "Datetime"})
		frappe.clear_messages()
		frappe.local.message_log = []


class TestEveryPublishedTypeIsOwned(unittest.TestCase):
	"""One matrix, no gaps: a fieldtype a schema can publish is either cast here or declared as
	decided elsewhere. A new fieldtype cannot arrive unowned and silently unenforced."""

	def test_every_frappe_fieldtype_has_a_rule(self):
		missing = sorted(
			ft for ft in set(data_fieldtypes) | set(table_fields) if ft not in _base.DECLARED_TYPES
		)
		self.assertEqual(missing, [], f"fieldtypes with no rule in DECLARED_TYPES: {missing}")

	def test_the_types_decided_elsewhere_name_where(self):
		for fieldtype, why in _base.TYPE_VALUE_DECIDED_ELSEWHERE.items():
			self.assertIn(fieldtype, _base.DECLARED_TYPES, fieldtype)
			self.assertTrue(why.strip(), fieldtype)


class TestAVerdictWeCannotSupportIsNotEmitted(unittest.TestCase):
	"""A mapped exception carrying no message is an internal fault, not a verdict about the caller."""

	def test_a_blank_mapped_exception_is_logged_and_answers_server_error(self):
		with patch.object(frappe, "log_error") as logged:
			code, http, message, _fields, _detail = _base._classify(frappe.PermissionError(), "probe")
		self.assertEqual((code, http), ("server_error", 500))
		self.assertTrue(logged.called, "a code with no sentence must leave a trace")
		self.assertNotIn("no reason was recorded", message)

	def test_an_authored_refusal_keeps_its_own_code_and_sentence(self):
		# The guard: real authz denials carry a sentence and must be untouched by the rule above.
		authored = frappe.PermissionError("The API key for x does not carry the Partner API User role.")
		with patch.object(frappe, "log_error") as logged:
			code, http, message, _f, _d = _base._classify(authored, "probe")
		self.assertEqual((code, http), ("forbidden", 403))
		self.assertIn("Partner API User", message)
		self.assertFalse(logged.called, "an authored refusal is not an internal fault")

	def test_a_named_field_refusal_still_carries_its_fields(self):
		e = frappe.ValidationError("`custom_dob` is declared as Date")
		e.fields = ["custom_dob"]
		code, http, _m, fields, _d = _base._classify(e, "probe")
		self.assertEqual((code, http, fields), ("validation_error", 400, ["custom_dob"]))


class TestFrameworkRefusalsAreClientErrors(unittest.TestCase):
	"""417 is frappe's OWN `ValidationError.http_status_code`. A framework ValidationError raised
	before any endpoint ran is a statement about the REQUEST, so it is a 400 — never `server_error`."""

	def test_frappe_still_spells_validation_error_as_417(self):
		self.assertEqual(frappe.ValidationError.http_status_code, 417)

	def test_a_417_is_answered_as_a_client_error(self):
		code, http, message = _base._normalise_partner_error(None, 417, None)
		self.assertEqual((code, http), ("bad_request", 400))
		self.assertNotIn("417", message)
		# The measured DX trap: most HTTP clients set this header by default, and the same GET
		# answered a clean 404 without it.
		self.assertIn("Content-Type", message)

	def test_a_malformed_json_body_still_says_so(self):
		code, http, message = _base._normalise_partner_error(None, 400, "JSONDecodeError")
		self.assertEqual((code, http), ("bad_request", 400))
		self.assertIn("JSON", message)

	def test_the_other_framework_statuses_are_unchanged(self):
		for status, expected in ((401, "unauthorized"), (403, "forbidden"), (404, "not_found"),
		                         (429, "rate_limited")):
			code, http, _m = _base._normalise_partner_error(None, status, None)
			self.assertEqual((code, http), (expected, status), status)


class TestStageIsNotPartnerWritable(unittest.TestCase):
	"""Stage is set by reps in the CRM. RESERVED_FIELDS is the existing structural pattern for
	exactly this — an API-layer concept only, touching no permlevel and no docfield flag."""

	def test_custom_substage_is_reserved(self):
		self.assertIn("custom_substage", _base.RESERVED_FIELDS)

	def test_custom_substage_is_not_writable(self):
		self.assertFalse(_base.is_writable("custom_substage"))

	def test_custom_substage_is_advertised_output_only(self):
		d = _base.field_descriptor("custom_substage", "Lead Stage", "Link", required=True)
		self.assertEqual(d["behavior"], _base.BEHAVIOR_OUTPUT_ONLY)
		self.assertFalse(d["required"])

	def test_lead_owner_the_precedent_is_still_reserved(self):
		self.assertIn("lead_owner", _base.RESERVED_FIELDS)

	def test_no_docfield_flag_is_touched(self):
		# The claim being pinned: reps are unaffected because nothing here reads or writes meta.
		field = frappe.get_meta("CRM Lead").get_field("custom_substage")
		self.assertFalse(field.read_only, "reserving an API field must not read_only the docfield")
		self.assertEqual(field.permlevel or 0, 0)
