# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""GATE 4 REGRESSION PIN: the lead-list filter scope must not silently reopen.

`test_grain_filter_options` proves the RULE behaviourally with real fixtures. This pins the two ways the
leak could come back without that test noticing:

  1. **The source changes.** The endpoint stops asking the SCOPED lead table — a master query or a
     permission bypass looks correct on a small test database while being wrong against real data. That
     is exactly how this class of leak was introduced: a picker pointed at the master.
  2. **The coverage narrows.** A grain axis stops being recognised as one. The first build named three
     fieldnames, and the two history Links (`custom_previous_program`, `custom_origin_vertical`) leaked
     the entire programme master purely because they were not on that list. The rule is now derived from
     the field meta — a Link whose TARGET is a grain master — so the pin that matters is that a NEW grain
     Link field is covered with NO code change. That is asserted here by creating one.

Deliberately small, and no source-scanning of the frontend: that pins text rather than behaviour and
fails on any honest refactor. The frontend's half is proven where it is observable — the Gate 3/4 browser
evidence, as a real scoped rep.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.lead.test_grain_filter_scope_pinned
"""
import ast
import inspect
import unittest

import frappe

from tatva_connect.lead import filters as lead_filters

_NEW_FIELD = "custom_zz_pin_grain_link"


class TestTheRuleIsMetaDerived(unittest.TestCase):
	"""Coverage follows the DATA MODEL, not a list of names."""

	def tearDown(self):
		name = f"CRM Lead-{_NEW_FIELD}"
		if frappe.db.exists("Custom Field", name):
			frappe.delete_doc("Custom Field", name, force=True, ignore_permissions=True)  # authz-ok: tier-c — test fixture teardown
		frappe.db.commit()
		frappe.clear_cache(doctype="CRM Lead")

	def test_a_new_grain_link_field_is_covered_with_no_code_change(self):
		"""THE pin. Add a Link to a grain master and it must be scoped immediately — nothing edited."""
		self.assertNotIn(_NEW_FIELD, lead_filters.grain_filter_fields())

		field = frappe.new_doc("Custom Field")
		field.dt, field.fieldname, field.label = "CRM Lead", _NEW_FIELD, "ZZ Pin Grain Link"
		field.fieldtype, field.options = "Link", "CRM Program"
		field.insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no session user
		frappe.db.commit()
		frappe.clear_cache(doctype="CRM Lead")

		self.assertIn(
			_NEW_FIELD, lead_filters.grain_filter_fields(),
			"a Link to a grain master must be a grain axis by the rule, not by being listed",
		)
		self.assertIn(
			_NEW_FIELD, lead_filters.grain_filter_options(),
			"every grain axis must get a key, or the frontend hands it back to the master picker",
		)

	def test_a_link_to_a_non_grain_doctype_is_not_covered(self):
		"""The rule discriminates: it is the TARGET that makes a field a grain axis, not the fieldname."""
		field = frappe.new_doc("Custom Field")
		field.dt, field.fieldname, field.label = "CRM Lead", _NEW_FIELD, "ZZ Pin Grain Link"
		field.fieldtype, field.options = "Link", "User"
		field.insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no session user
		frappe.db.commit()
		frappe.clear_cache(doctype="CRM Lead")

		self.assertNotIn(_NEW_FIELD, lead_filters.grain_filter_fields())

	def test_the_history_links_are_covered_by_the_same_rule(self):
		"""Finding 2 closes as a consequence: these are Links to a grain master, so they are grain axes."""
		fields = lead_filters.grain_filter_fields()
		for fieldname in ("custom_previous_program", "custom_origin_vertical"):
			self.assertIn(fieldname, fields)

	def test_no_hardcoded_fieldname_list_survives(self):
		"""A reintroduced allowlist is the defect itself — it is how the history fields were missed."""
		source = inspect.getsource(lead_filters)
		self.assertNotIn("GRAIN_FIELDS", source)
		for fieldname in ("custom_vertical", "custom_group", "custom_current_program"):
			self.assertNotIn(
				f'"{fieldname}"', source,
				"grain fieldnames must be derived from the meta, never named here",
			)


class TestTheSourceStaysScoped(unittest.TestCase):
	"""The values keep coming from the caller's own, permission-scoped lead query."""

	def setUp(self):
		self.source = inspect.getsource(lead_filters)
		self.calls = [n for n in ast.walk(ast.parse(self.source)) if isinstance(n, ast.Call)]

	def _named(self, name):
		return [
			n for n in self.calls
			if (getattr(n.func, "attr", None) or getattr(n.func, "id", None)) == name
		]

	def test_the_options_come_from_the_scoped_lead_table(self):
		"""`get_list` is what applies User Permission — that IS the mechanism, not a detail."""
		self.assertTrue(self._named("get_list"))

	def test_no_grain_master_is_ever_queried(self):
		"""Naming the masters is the RULE (which target makes a grain axis); querying one is the LEAK."""
		queried = [
			arg.value
			for call in self._named("get_list") + self._named("get_all")
			for arg in call.args
			if isinstance(arg, ast.Constant)
		]
		for master in lead_filters.GRAIN_MASTERS:
			self.assertNotIn(master, queried)

	def test_permissions_are_never_bypassed(self):
		"""`get_all`, `ignore_permissions` and raw SQL all skip User Permission — any of them unscopes it."""
		self.assertNotIn("ignore_permissions", self.source)
		self.assertNotIn("frappe.db.sql", self.source)
		self.assertFalse(self._named("get_all"))


if __name__ == "__main__":
	unittest.main()
