# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The frontend's control matrix is enumerated from a FIXTURE, and this is the lock that it is not stale.

The matrix suite asserts a contract per CONTROL and applies it to every declared field, refusing to run at
all if a tuple has no contract. That only means something while the tuple list is the registry's own. A
fixture is used rather than a live call so a component test needs no bench, and this test is the price of
that: the moment a node type or a config field is added, renamed or retyped, the fixture is stale and this
goes red with the exact difference.

Regenerate with `bench --site <site> execute
tatva_connect.workflow_engine.tests.test_control_matrix_fixture.regenerate`.
"""
import json
import pathlib

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.workflow_engine import registry

_FIXTURE = (
	pathlib.Path(frappe.get_app_path("crm")).parent
	/ "frontend" / "tests" / "component" / "_nodeTypes.fixture.json"
)


def _shape():
	"""The wire payload itself. The fixture IS what the endpoint sends, so a component test renders exactly
	what production renders — a trimmed view could not, and a panel mounted against it drew nothing while
	the test passed."""
	return json.loads(json.dumps(registry.node_types(), default=str))


def regenerate():
	"""Rewrite the fixture from the live registry — the ONE way it is produced."""
	_FIXTURE.write_text(json.dumps(
		{"generated_from": "tatva_connect.workflow_engine.registry.node_types", "node_types": _shape()},
		indent=1,
	) + "\n")
	return str(_FIXTURE)


class TestTheControlMatrixFixtureIsNotStale(FrappeTestCase):
	def test_the_fixture_matches_the_live_declaration(self):
		if not _FIXTURE.exists():
			self.skipTest("the crm frontend is not checked out beside this app")
		held = json.loads(_FIXTURE.read_text())["node_types"]
		live = _shape()
		self.assertEqual(
			{n["type"] for n in held}, {n["type"] for n in live},
			"a node type was added or removed — regenerate the matrix fixture",
		)
		for node in live:
			with self.subTest(node=node["type"]):
				self.assertEqual(
					next(n for n in held if n["type"] == node["type"]), node,
					f"{node['type']} drifted from the fixture — regenerate it",
				)
