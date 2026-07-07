# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Task 1 sign-off - the v2 trigger vocabulary is frozen in the CRM Automation Rule schema.

Real Frappe meta as the oracle (frappe.get_meta), no hardcoded verdict beyond the frozen
vocabulary itself (Part A of the plan). The old trigger_type/task_type/watch_doctype/watch_field
fields must be GONE - this is a replace, not an add-alongside (invariant A.8 / no parallel brains).
"""
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

_DT = "CRM Automation Rule"


class TestRuleSchemaEventOptions(FrappeTestCase):
	def test_event_options(self):
		meta = frappe.get_meta(_DT)
		event = meta.get_field("event")
		self.assertIsNotNone(event, "CRM Automation Rule has no 'event' field")
		options = [o for o in (event.options or "").split("\n") if o]
		self.assertEqual(options, ["Created", "Updated", "Deleted"])

	def test_on_doctype_is_link_to_doctype(self):
		meta = frappe.get_meta(_DT)
		on_doctype = meta.get_field("on_doctype")
		self.assertIsNotNone(on_doctype, "CRM Automation Rule has no 'on_doctype' field")
		self.assertEqual(on_doctype.fieldtype, "Link")
		self.assertEqual(on_doctype.options, "DocType")

	def test_old_trigger_vocabulary_is_gone(self):
		"""Big-bang reshape (invariant A.8): the old fields do not coexist with the new ones."""
		meta = frappe.get_meta(_DT)
		for stale in ("trigger_type", "task_type", "watch_doctype", "watch_field"):
			self.assertIsNone(meta.get_field(stale), f"stale trigger field {stale!r} still on the schema")


class TestCriterionOperatorVocabulary(FrappeTestCase):
	def test_operator_options_match_frozen_set(self):
		meta = frappe.get_meta("CRM Automation Criterion")
		operator = meta.get_field("operator")
		options = [o for o in (operator.options or "").split("\n") if o]
		self.assertEqual(
			options,
			[
				"is", "is not", "greater than", "less than", "at least", "at most",
				"is one of", "is not one of", "contains", "does not contain",
				"is set", "is not set", "is between", "changed to", "changed from…to",
			],
		)


class TestActionVerbVocabulary(FrappeTestCase):
	def test_action_type_options_match_frozen_set(self):
		meta = frappe.get_meta("CRM Automation Action")
		action_type = meta.get_field("action_type")
		options = [o for o in (action_type.options or "").split("\n") if o]
		self.assertEqual(
			options,
			[
				"Require Fields", "Require Location", "Update Field", "Create Task",
				"Create Note", "Send WhatsApp", "Send Email", "Call Webhook", "Wait",
			],
		)

	def test_new_verb_fields_exist(self):
		meta = frappe.get_meta("CRM Automation Action")
		for fieldname, fieldtype in (
			("wait_expression", "Small Text"),
			("email_recipient", "Data"),
			("email_subject", "Data"),
			("email_body", "Small Text"),
			("whatsapp_template", "Link"),
			("require_fields", "Small Text"),
			("geofence_meters", "Int"),
		):
			df = meta.get_field(fieldname)
			self.assertIsNotNone(df, f"CRM Automation Action has no {fieldname!r} field")
			self.assertEqual(df.fieldtype, fieldtype, f"{fieldname} should be {fieldtype}")
		self.assertEqual(meta.get_field("whatsapp_template").options, "WhatsApp Templates")


if __name__ == "__main__":
	unittest.main()
