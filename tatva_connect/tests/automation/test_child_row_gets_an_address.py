# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A row an automation node appends to a multi-row section carries an address.

A multi-row section addresses its rows by the column its `row_key_field` names. A row with a blank key has
no address at all: the partner API can never target it and every later write lands beside it — or, worse,
merges into it, because `row_for_section` hands the same keyless row back for ever.

The partner lane stamped such a row on arrival; the automation lane did not, so `Append Child Row` and
`Upsert Child Row` into an empty section wrote keyless rows. On prod that is 752 acquisition rows created
AFTER the historical stamping patch had already run.

Driven through `interpreter._run_verb`, the real executor, and asserted on the lead's own child table.
A test that asserted `stamp_row_key` was called would stay green with the verbs still bypassing it.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.automation.test_child_row_gets_an_address
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.tests.authz.grains import assert_masters_exist
from tatva_connect.tests.automation import field_allowlist
from tatva_connect.workflow_engine import interpreter, refs
from tatva_connect.workflow_engine.tests import fixtures as fx

_SECTION = "acq"
_TABLE = "custom_acquisition_profile"  # the `acq` section's child table, per the CRM Lead Section brain
_FIELD = "utm_source"
_AUTHORED_KEY = "2020-01-02 03:04:05"


class TestChildRowGetsAnAddress(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		assert_masters_exist()
		cls.section = frappe.get_cached_doc("CRM Lead Section", _SECTION)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		field_allowlist.clear("CRM Lead")
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		for fieldname in (_FIELD, self.section.row_key_field):
			field_allowlist.seed_settable(
				"CRM Lead", fieldname,
				vertical=fx.GRAIN["vertical"], group=fx.GRAIN["group"], program=fx.GRAIN["program"],
			)
		self.lead = fx.make_lead()

	def _run(self, node_type, values):
		node = frappe._dict(
			node_id="c1",
			node_type=node_type,
			config_json=frappe.as_json({
				"child_table": _TABLE,
				"set_fields": [{"name": k, "mode": refs.LITERAL, "value": v} for k, v in values.items()],
			}),
		)
		interpreter._run_verb(node, self.lead.name, None, refs.Values(), fx.AXES)
		return frappe.get_doc("CRM Lead", self.lead.name).get(_TABLE) or []

	def test_append_child_row_stamps_the_row_key(self):
		"""The section declares a row key and the node's Field Map named none, so the row is timed."""
		rows = self._run("Append Child Row", {_FIELD: "wf-probe"})

		self.assertEqual(len(rows), 1)
		self.assertTrue(rows[0].get(self.section.row_key_field), "the appended row carries no address")

	def test_upsert_child_row_stamps_the_row_it_appends(self):
		"""Upsert's OTHER branch: with no row to write, it appends one, and that row needs an address too."""
		rows = self._run("Upsert Child Row", {_FIELD: "wf-probe"})

		self.assertEqual(len(rows), 1)
		self.assertTrue(rows[0].get(self.section.row_key_field), "the upserted row carries no address")

	def test_a_second_node_addresses_the_first_row_instead_of_merging_into_it(self):
		"""What the blank key actually cost: two appends must be two rows, not one row written twice."""
		self._run("Append Child Row", {_FIELD: "first"})
		rows = self._run("Append Child Row", {_FIELD: "second"})

		self.assertEqual(len(rows), 2, "the second append landed on the first row")
		self.assertEqual(len({r.get(self.section.row_key_field) for r in rows}), 2, "both rows share one address")

	def test_an_authored_row_key_is_never_re_dated(self):
		"""A node that sets the key itself keeps it — this gives a row an address, it does not re-date one."""
		rows = self._run("Append Child Row", {_FIELD: "wf-probe", self.section.row_key_field: _AUTHORED_KEY})

		self.assertEqual(len(rows), 1)
		self.assertEqual(str(rows[0].get(self.section.row_key_field)), _AUTHORED_KEY)
