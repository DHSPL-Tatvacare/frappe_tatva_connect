# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Update Field given the label its picker offers writes the key the column holds.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.automation.test_update_field_writes_the_key
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.taxonomy import labels
from tatva_connect.tests.authz.grains import assert_masters_exist
from tatva_connect.tests.automation import field_allowlist
from tatva_connect.workflow_engine import interpreter, refs
from tatva_connect.workflow_engine.tests import fixtures as fx

_FIELD = "custom_substage"


def _a_stage():
	"""A selectable stage of the fixture programme, carrying the label the picker's own query offers."""
	row = frappe.db.get_value(
		labels.LEAD_STAGE, {"program": fx.GRAIN["program"], "selectable": 1},
		["name", "stage"], as_dict=True, order_by="position asc",
	)
	if not row:
		raise AssertionError(f"no stage for programme {fx.GRAIN['program']!r} — the fixture is wrong")
	row.label = labels.label(row.name, labels.LEAD_STAGE)
	offered = [value for value, _shown in labels.label_query(labels.LEAD_STAGE, row.label, "", 0, 50, {})]
	if row.label not in offered:
		raise AssertionError(f"the picker does not offer {row.label!r} — the fixture is wrong")
	return row


class TestUpdateFieldWritesTheKey(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		assert_masters_exist()
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		field_allowlist.clear("CRM Lead")
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		field_allowlist.seed_settable(
			"CRM Lead", _FIELD,
			vertical=fx.GRAIN["vertical"], group=fx.GRAIN["group"], program=fx.GRAIN["program"],
		)
		self.lead = fx.make_lead()
		self.stage = _a_stage()

	def _run(self, value):
		node = frappe._dict(
			node_id="u1",
			node_type="Update Field",
			config_json=frappe.as_json({
				"target_doctype": "CRM Lead",
				"updates": [{"name": _FIELD, "mode": refs.LITERAL, "value": value}],
			}),
		)
		interpreter._run_verb(node, self.lead.name, None, refs.Values(), fx.AXES)
		return frappe.db.get_value("CRM Lead", self.lead.name, _FIELD)

	def test_the_picked_label_writes_the_programme_key(self):
		"""The defect: a picked label reached the Link column as a label and failed every run."""
		self.assertEqual(self._run(self.stage.label), self.stage.name)

	def test_a_key_is_written_untouched(self):
		"""Every live workflow stores keys; they must reach the column as they are."""
		self.assertEqual(self._run(self.stage.name), self.stage.name)

	def test_a_label_the_programme_lacks_fails_the_step_and_names_the_field(self):
		"""No match is a failed step that says why, never a silent skip."""
		with self.assertRaises(ValueError) as caught:
			self._run("zz-no-such-stage")

		self.assertIn(_FIELD, str(caught.exception))
		self.assertFalse(frappe.db.get_value("CRM Lead", self.lead.name, _FIELD), "a failed step still wrote")
