# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A condition holding a task type's NAME, as the picker offers it, matches the task carrying that type's key."""

import frappe
from frappe.tests import IntegrationTestCase

from tatva_connect.automation import context, rules
from tatva_connect.tests.authz.grains import GRAINS

_FIELD = "crm_task.custom_task_type"


def _rule(operator, value, from_value=None):
	return frappe._dict(type="rule", field=_FIELD, operator=operator, value=value, from_value=from_value)


def _task_type(name):
	grain = GRAINS[2]
	return frappe.get_doc({"doctype": "CRM Task Type", "type_name": name, "vertical": grain["vertical"],
	                       "group": grain["group"], "program": grain["program"]}).insert(ignore_permissions=True).name  # authz-ok: tier-c — test fixture


class TestAPickedNameMatchesItsRecord(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.fields = context.fields_for("CRM Task", "CRM Lead")
		suffix = frappe.generate_hash(length=6)
		cls.name, cls.was = f"ZZ Picked {suffix}", f"ZZ Other {suffix}"
		cls.key, cls.other = _task_type(cls.name), _task_type(cls.was)

	def _match(self, rule, now, before=None):
		ctx = {_FIELD: now} if before is None else {_FIELD: now, f"{_FIELD}__before": before}
		return rules.predicate_match(rule, ctx, self.fields)

	def test_is_and_is_not_read_the_name_as_its_key(self):
		self.assertTrue(self._match(_rule("is", self.name), self.key))
		self.assertFalse(self._match(_rule("is", self.name), self.other))
		self.assertFalse(self._match(_rule("is not", self.name), self.key))
		self.assertTrue(self._match(_rule("is not", self.name), self.other))

	def test_is_one_of_reads_each_name_as_its_key(self):
		self.assertTrue(self._match(_rule("is one of", f"Not a type\n{self.name}"), self.key))
		self.assertFalse(self._match(_rule("is one of", "Not a type"), self.key))
		self.assertFalse(self._match(_rule("is not one of", f"{self.was}\n{self.name}"), self.key))

	def test_changed_to_and_from_read_the_name_as_its_key(self):
		self.assertTrue(self._match(_rule("changed to", self.name), self.key, self.other))
		self.assertFalse(self._match(_rule("changed to", self.was), self.key, self.other))
		self.assertTrue(self._match(_rule("changed from…to", self.name, self.was), self.key, self.other))
		self.assertFalse(self._match(_rule("changed from…to", self.was, self.name), self.key, self.other))

	def test_a_stored_key_and_typed_text_answer_as_before(self):
		self.assertTrue(self._match(_rule("is", self.key), self.key))
		self.assertTrue(self._match(_rule("changed to", self.key), self.key, self.other))
		self.assertTrue(self._match(_rule("is one of", f"{self.other}\n{self.key}"), self.key))
