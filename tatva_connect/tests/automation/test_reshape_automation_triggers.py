# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Task 1 sign-off - patches.reshape_automation_triggers.

By test time `bench migrate` has already run, so the DB no longer carries the old trigger_type/
task_type/watch_doctype/watch_field columns (the v2 JSON dropped them). To exercise the patch we
resurrect those columns on a throwaway row via raw DDL (simulating a not-yet-migrated site), run the
patch directly, and assert the v2 shape - real Frappe engine + real SQL as the oracle, no mocked
verdict (S.6).
"""
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.patches import reshape_automation_triggers as patch
from tatva_connect.tests.authz.grains import GRAINS, assert_masters_exist

_RULE = "CRM Automation Rule"
_RULE_TABLE = "tab" + _RULE
_CRITERION = "CRM Automation Criterion"
_CRITERION_TABLE = "tab" + _CRITERION
_GRAIN = GRAINS[0]

_OLD_COLUMNS = ("trigger_type", "task_type", "watch_doctype", "watch_field")


def _add_old_columns():
	for col in _OLD_COLUMNS:
		if not patch._column_exists(_RULE_TABLE, col):
			frappe.db.sql_ddl(f"ALTER TABLE `{_RULE_TABLE}` ADD COLUMN `{col}` varchar(140)")


def _drop_old_columns():
	for col in _OLD_COLUMNS:
		if patch._column_exists(_RULE_TABLE, col):
			frappe.db.sql_ddl(f"ALTER TABLE `{_RULE_TABLE}` DROP COLUMN `{col}`")


def _make_v2_rule(name, **extra):
	"""A real, valid v2 rule via the ORM (exercises the reshaped validate() normally)."""
	doc = frappe.get_doc({
		"doctype": _RULE, "rule_name": name, "enabled": 0,
		"on_doctype": "CRM Lead", "event": "Updated",
		"vertical": _GRAIN["vertical"], "group": _GRAIN["group"], "program": _GRAIN["program"],
		**extra,
	})
	doc.insert(ignore_permissions=True)
	return doc


def _revert_to_old_shape(rule_name, trigger_type, task_type=None, watch_doctype=None, watch_field=None):
	"""Blank the v2 fields and populate the (resurrected) old ones via raw SQL - simulating a
	not-yet-migrated row without going through Document.save() (which would re-validate as v2)."""
	frappe.db.sql(
		f"""UPDATE `{_RULE_TABLE}` SET on_doctype=NULL, event=NULL,
		    trigger_type=%(trigger_type)s, task_type=%(task_type)s,
		    watch_doctype=%(watch_doctype)s, watch_field=%(watch_field)s
		    WHERE name=%(name)s""",
		{
			"trigger_type": trigger_type, "task_type": task_type,
			"watch_doctype": watch_doctype, "watch_field": watch_field, "name": rule_name,
		},
	)


class TestReshapeAutomationTriggers(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		assert_masters_exist()

	def setUp(self):
		_add_old_columns()

	def tearDown(self):
		frappe.db.delete(_RULE, {"rule_name": ("like", "ReshapeProbe-%")})
		_drop_old_columns()

	# (a) Task Completed -> on_doctype=CRM Task, event=Updated, PREPEND `status changed to Done`.
	def test_task_completed_migrates_and_prepends_criterion(self):
		rule = _make_v2_rule("ReshapeProbe-tc")
		_revert_to_old_shape(rule.name, trigger_type="Task Completed", task_type="dummy")
		patch.execute()
		row = frappe.db.get_value(_RULE, rule.name, ["on_doctype", "event"], as_dict=True)
		self.assertEqual(row.on_doctype, "CRM Task")
		self.assertEqual(row.event, "Updated")
		criteria = frappe.get_all(
			_CRITERION, filters={"parent": rule.name, "parenttype": _RULE},
			fields=["field", "operator", "value", "idx"], order_by="idx asc",
		)
		self.assertTrue(criteria, "no criterion prepended")
		self.assertEqual(criteria[0].field, "status")
		self.assertEqual(criteria[0].operator, "changed to")
		self.assertEqual(criteria[0].value, "Done")

	# (b) Field Changed -> on_doctype=<watch_doctype>, event=Updated. No criterion prepended.
	def test_field_changed_migrates_on_doctype(self):
		rule = _make_v2_rule("ReshapeProbe-fc")
		_revert_to_old_shape(rule.name, trigger_type="Field Changed", watch_doctype="CRM Lead", watch_field="custom_stage")
		patch.execute()
		row = frappe.db.get_value(_RULE, rule.name, ["on_doctype", "event"], as_dict=True)
		self.assertEqual(row.on_doctype, "CRM Lead")
		self.assertEqual(row.event, "Updated")
		criteria = frappe.get_all(_CRITERION, filters={"parent": rule.name, "parenttype": _RULE})
		self.assertEqual(len(criteria), 0, "Field Changed migration must not prepend a criterion")

	# (c) IDEMPOTENT: a second run does not duplicate the prepended criterion or re-touch the row.
	def test_second_run_is_idempotent(self):
		rule = _make_v2_rule("ReshapeProbe-idem")
		_revert_to_old_shape(rule.name, trigger_type="Task Completed", task_type="dummy")
		patch.execute()
		patch.execute()  # second run - old columns still present (this test doesn't drop them mid-test)
		criteria = frappe.get_all(_CRITERION, filters={"parent": rule.name, "parenttype": _RULE, "field": "status", "operator": "changed to", "value": "Done"})
		self.assertEqual(len(criteria), 1, "idempotency broken - the prepended criterion duplicated")

	# (d) ROW COUNT RECONCILED: the patch never changes the total row count.
	def test_row_count_reconciled(self):
		before = frappe.db.count(_RULE)
		rule = _make_v2_rule("ReshapeProbe-count")
		_revert_to_old_shape(rule.name, trigger_type="Field Changed", watch_doctype="CRM Lead", watch_field="custom_stage")
		patch.execute()
		after = frappe.db.count(_RULE)
		self.assertEqual(after, before + 1, "row count drifted across the migration")

	# (e) KNOWN-BAD PLANT (S.6 recall): an already-migrated row (on_doctype already set) is left
	# alone - the patch's WHERE clause must not re-touch it even if trigger_type is still present
	# (a defensive no-op, not a silent overwrite).
	def test_already_migrated_row_is_untouched(self):
		rule = _make_v2_rule("ReshapeProbe-already", on_doctype="CRM Task", event="Updated")
		# Plant the bad: old columns present with a DIFFERENT (wrong) value, on_doctype already set.
		frappe.db.sql(
			f"UPDATE `{_RULE_TABLE}` SET trigger_type='Field Changed', watch_doctype='CRM Task' WHERE name=%s",
			(rule.name,),
		)
		patch.execute()
		row = frappe.db.get_value(_RULE, rule.name, ["on_doctype", "event"], as_dict=True)
		self.assertEqual(row.on_doctype, "CRM Task", "an already-migrated row was overwritten - recall < 1.0")
		self.assertEqual(row.event, "Updated")


class TestReshapeCriterionOperators(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		assert_masters_exist()

	def tearDown(self):
		frappe.db.delete(_RULE, {"rule_name": ("like", "ReshapeProbe-%")})

	# (f) old operator SYMBOLS remap to the frozen v2 WORD operators.
	def test_operator_symbols_remap_to_words(self):
		rule = _make_v2_rule(
			"ReshapeProbe-ops",
			criteria=[{"field": "custom_stage", "operator": "is", "value": "New"}],
		)
		# Overwrite the (already-word) operator back to an old SYMBOL via raw SQL (bypassing the
		# native Select validation) to simulate a not-yet-remapped row.
		crit_name = frappe.get_all(_CRITERION, filters={"parent": rule.name, "parenttype": _RULE}, pluck="name")[0]
		frappe.db.sql(f"UPDATE `{_CRITERION_TABLE}` SET operator='=' WHERE name=%s", (crit_name,))
		patch.execute()
		self.assertEqual(frappe.db.get_value(_CRITERION, crit_name, "operator"), "is")

	# (g) a value already in word form is left untouched (idempotent — not in the old-symbol map).
	def test_word_operator_is_untouched(self):
		rule = _make_v2_rule(
			"ReshapeProbe-opsword",
			criteria=[{"field": "custom_stage", "operator": "is not", "value": "New"}],
		)
		crit_name = frappe.get_all(_CRITERION, filters={"parent": rule.name, "parenttype": _RULE}, pluck="name")[0]
		patch.execute()
		self.assertEqual(frappe.db.get_value(_CRITERION, crit_name, "operator"), "is not")


if __name__ == "__main__":
	unittest.main()
