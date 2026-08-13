# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The Deal surfaces — every one of them reads the patient THROUGH `deal.lead`, and copies nothing.

What is asserted:

  * a Deal resolves to its lead as an automation subject, which is the SAME resolver the workflow engine
    reads through — so one entry answers both, and there is no second map to keep in step;
  * the Data tab of a deal is its LEAD's panel: the same sections, the resolved lead named back, and
    read-only, because `update_lead_detail`'s allowlist is built for a lead;
  * that panel refuses on the LEAD, not merely on the deal — reaching a patient through a deal id can
    never show more than reaching them through the lead id would;
  * a deal is findable in the spotlight index and its hit routes to the DEAL, not to a lead tab;
  * `CRM Vertical.deals_enabled` is the ONE fact behind every deal-shaped option: a line that does not
    sell offers no Deal subject and refuses a workflow scoped to it.

Fixtures are `test_deal_guards`' own — the same armed/unarmed lines, group, programme and buying stage.
There is one fixture set for deals, not two.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.deal.test_deal_surfaces
"""
import frappe

from tatva_connect.access import surfaces
from tatva_connect.automation import subjects
from tatva_connect.lead import detail
from tatva_connect.search import index as search_index
from tatva_connect.tests.deal.test_deal_guards import ARMED, UNARMED, TestDealGuards
from tatva_connect.workflow_engine import registry


class TestDealSurfaces(TestDealGuards):
	"""Extends the guards' fixtures rather than building a second set — same lines, same stage, same switch."""

	# ---- the subject resolver ----------------------------------------------------------------------

	def test_a_deal_resolves_to_its_lead_as_an_automation_subject(self):
		lead = self._lead()
		deal = self._deal(lead.name)
		self.assertEqual(subjects.resolve_lead_name(deal), lead.name,
						 "a deal did not resolve to the lead it came from, so no rule can see its grain")

	def test_the_workflow_engine_reads_the_same_resolver(self):
		"""One map, both engines: the workflow front-door resolves its subject through `automation.context`,
		which delegates to `subjects.resolve_lead_name`. Asserting the identity is what stops a second map."""
		from tatva_connect.automation import context as ctx_build

		lead = self._lead()
		deal = self._deal(lead.name)
		self.assertEqual(ctx_build.subject(deal).name, lead.name,
						 "the workflow engine resolved a deal to something other than its lead")
		self.assertIn("CRM Deal", registry._subject_options(),
					  "a Deal cannot be picked as a workflow subject at all")

	# ---- the Data tab ------------------------------------------------------------------------------

	def test_a_deals_panel_is_its_leads_panel(self):
		lead = self._lead()
		deal = self._deal(lead.name)
		through_lead = detail.lead_detail(lead.name)
		through_deal = detail.lead_detail(deal.name, doctype="CRM Deal")
		self.assertEqual([s["key"] for s in through_deal["sections"]],
						 [s["key"] for s in through_lead["sections"]],
						 "a deal was served different sections from the lead it is about")
		self.assertEqual(through_deal["lead"], lead.name,
						 "the panel did not name the lead it resolved, so its history and rows reads address a deal id")

	def test_a_deals_panel_is_read_only_and_says_so(self):
		"""Read-only is DECLARED, not merely enforced: the panel hides Edit rather than failing on Save."""
		lead = self._lead()
		deal = self._deal(lead.name)
		self.assertTrue(detail.lead_detail(deal.name, doctype="CRM Deal")["read_only"],
						"a deal's panel offered an edit that the write path is not built for")
		self.assertFalse(detail.lead_detail(lead.name)["read_only"],
						 "the lead's own panel lost its edit")

	def test_a_deal_whose_lead_cannot_be_read_is_refused(self):
		"""The gate is on the LEAD. A deal id is otherwise a way to read a patient you cannot otherwise see."""
		lead = self._lead()
		deal = self._deal(lead.name)
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			detail.lead_detail(deal.name, doctype="CRM Deal")

	def test_an_unsupported_record_type_is_refused(self):
		"""Fail-closed: the hop admits exactly the two record types whose Data tab this projection serves."""
		lead = self._lead()
		with self.assertRaises(frappe.ValidationError):
			detail.resolve_panel_lead(lead.name, doctype="CRM Task")

	# ---- the spotlight -----------------------------------------------------------------------------

	def test_a_deal_is_indexed_against_its_lead(self):
		"""The row is the same patient: the index resolves a deal to its lead, and a hit opens the DEAL."""
		lead = self._lead()
		deal = self._deal(lead.name)
		engine = search_index.CRMLeadSearch()
		self.assertEqual(engine._lead_of(deal), lead.name,
						 "a deal row would carry no patient, so it could never be found or permission-scoped")
		self.assertIn("CRM Deal", engine.INDEXABLE_DOCTYPES,
					  "the deal tier is not declared, so no deal is ever indexed")
		self.assertIsNone(search_index.TAB["CRM Deal"],
						  "a deal hit was routed to a lead tab; a deal is not a child of a lead")

	# ---- one fact behind every deal-shaped option ---------------------------------------------------

	def test_deals_enabled_is_the_one_gate(self):
		"""Same function, every surface — and a blank vertical is the WILDCARD, never a miss."""
		self.assertTrue(surfaces.deals_enabled(ARMED), "an armed line did not read as selling")
		self.assertFalse(surfaces.deals_enabled(UNARMED), "an unarmed line read as selling")
		self.assertTrue(surfaces.deals_enabled(""), "a blank vertical stopped being the wildcard")

	def test_a_line_that_does_not_sell_offers_no_deal_subject(self):
		self.assertNotIn("CRM Deal", registry.subject_options(UNARMED),
						 "a line that does not sell offered Deal as a workflow subject")
		self.assertIn("CRM Deal", registry.subject_options(ARMED),
					  "a line that sells did not offer Deal as a workflow subject")

	def test_a_workflow_scoped_to_a_line_that_does_not_sell_cannot_watch_a_deal(self):
		"""The authoritative per-grain gate: the picker asks the wildcard, this sees grain and subject together."""
		workflow = frappe.get_doc({"doctype": "CRM Workflow", "title": "ZZ Deal Subject Probe"})
		workflow.trigger_doctype = "CRM Deal"
		workflow.trigger_vertical = UNARMED
		with self.assertRaises(frappe.ValidationError):
			workflow.deal_subject_needs_a_selling_line()
		workflow.trigger_vertical = ARMED
		workflow.deal_subject_needs_a_selling_line()
