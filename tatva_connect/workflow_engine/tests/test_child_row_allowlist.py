# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Append Child Row really lands a row, and really refuses a field this grain is not entitled to.

`_assert_child_in_grain` asked `fields.is_settable` about the CHILD doctype. `is_settable` handles the
Task catalogs and the lead catalog and returns False for everything else, so every Append/Upsert Child Row
node raised PermissionError on its first field — the verb had never worked. The gate was right; the
question was addressed to the wrong doctype.

Driven through `interpreter._run_verb`, the real executor, and asserted on the lead's own child table —
never on a call. A test that asserted the handler was invoked would have stayed green throughout.
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.tests.authz.grains import assert_masters_exist
from tatva_connect.tests.automation import field_allowlist
from tatva_connect.workflow_engine import interpreter, refs
from tatva_connect.workflow_engine.tests import fixtures as fx

_TABLE = "custom_acquisition_profile"  # the `acq` section's child table, per the CRM Lead Section brain
_ALLOWED = "utm_source"                # ticked into this test's grain contract by setUp
_FORBIDDEN = "custom_signed_up_on_app"  # a real column on the same child doctype, entitled to no grain here


class TestChildRowAllowlist(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		cls.lead = fx.make_lead()
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		field_allowlist.clear("CRM Lead")
		frappe.delete_doc("CRM Lead", cls.lead.name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def setUp(self):
		field_allowlist.seed_settable(
			"CRM Lead", _ALLOWED,
			vertical=fx.GRAIN["vertical"], group=fx.GRAIN["group"], program=fx.GRAIN["program"],
		)

	def _node(self, values):
		return frappe._dict(
			node_id="c1",
			node_type="Append Child Row",
			config_json=frappe.as_json({
				"child_table": _TABLE,
				"set_fields": [{"name": k, "mode": refs.LITERAL, "value": v} for k, v in values.items()],
			}),
		)

	def test_an_allowlisted_child_field_really_lands_a_row(self):
		"""The outcome, not the call: the lead itself carries the new row after the verb runs."""
		before = len(frappe.get_doc("CRM Lead", self.lead.name).get(_TABLE) or [])

		interpreter._run_verb(self._node({_ALLOWED: "wf-probe"}), self.lead.name, None, refs.Values(), fx.AXES)

		rows = frappe.get_doc("CRM Lead", self.lead.name).get(_TABLE) or []
		self.assertEqual(len(rows), before + 1, "Append Child Row did not add a row to the lead")
		self.assertEqual(rows[-1].get(_ALLOWED), "wf-probe")

	def test_a_field_outside_the_grain_is_refused(self):
		"""The fail-closed direction. Fixing the doctype the gate is asked about must not open the gate."""
		with self.assertRaises(PermissionError):
			interpreter._run_verb(self._node({_FORBIDDEN: "wf-probe"}), self.lead.name, None, refs.Values(), fx.AXES)

		rows = frappe.get_doc("CRM Lead", self.lead.name).get(_TABLE) or []
		self.assertFalse(
			[r for r in rows if r.get(_FORBIDDEN) == "wf-probe"],
			"a refused field still reached the lead",
		)
