# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""`changed to` on an unwatched field can never match, and publish now says so.

Only a WATCHED field has its before-value captured (`automation.context.diff_watched_fields`), so a
transition operator on any other field is dead on every record — green at publish, silent for ever. Four of
the six subjects carry no field catalog at all, so every transition an author could build on a Deal, a
File, a WhatsApp Message or a Call Log was already dead the day it was written.

`fields.is_watchable` was written for exactly this question and had no caller anywhere; the publish gate is
now it. The same disease as the rest of this leg: the offer was wider than the gate, and the gap was silent.
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import fields
from tatva_connect.workflow_engine import graph

_DEAD = "never match"


def _problems(subject, ref, operator="changed to"):
	nodes = [
		{"node_id": "trg", "node_type": "Trigger", "edges": [{"from_output": "next", "to_node": "t1"}],
		 "config": {"mode": "Record Event", "subject_doctype": subject, "event": "Updated",
		            "predicate": {"type": "all", "children": [
		                {"type": "rule", "field": ref, "operator": operator, "value": "x"}]}}},
		{"node_id": "t1", "node_type": "Terminal", "edges": [], "config": {}},
	]
	return [p["message"] for p in graph.problems(nodes) if _DEAD in p["message"]]


class TestATransitionNeedsABeforeValue(FrappeTestCase):
	def test_a_subject_with_no_catalog_cannot_carry_a_transition(self):
		"""The case that was silent: File has no field catalog, so nothing records what anything held."""
		self.assertTrue(_problems("File", "file.file_name"))

	def test_both_transition_operators_are_covered(self):
		"""`changed from…to` reads the same before-value and was equally dead."""
		self.assertTrue(_problems("File", "file.file_name", "changed from…to"))

	def test_a_watched_field_is_left_alone(self):
		"""The other direction. A gate that refused everything would pass the tests above too."""
		if not fields.is_watchable("CRM Lead", "status"):
			self.skipTest("no watchable lead field on this bench — see the catalog seed")
		self.assertEqual(_problems("CRM Lead", "crm_lead.status"), [])

	def test_the_lead_is_reachable_from_another_subject(self):
		"""A Task-subject workflow may test the LEAD's transition, and the check must resolve that slug."""
		if not fields.is_watchable("CRM Lead", "status"):
			self.skipTest("no watchable lead field on this bench")
		self.assertEqual(_problems("CRM Task", "crm_lead.status"), [])

	def test_an_ordinary_operator_is_never_touched(self):
		"""`is` reads the live record and needs no before-value, whatever the catalog says."""
		self.assertEqual(_problems("File", "file.file_name", "is"), [])
