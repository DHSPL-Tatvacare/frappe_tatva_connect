# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A Trigger sees the record that fired AND the lead it belongs to — the same pair a Route sees.

THE RED. `_trigger_context` built its context from the firing document alone and asked
`field_types_for` about that one doctype, so a predicate naming the patient was in neither the values
nor the declaration and `rules._rule_match` raised. The Route was given both records and evaluated the
identical rule fine — one sentence, two meanings, depending which node you stood in.

It is not a corner: the picker OFFERS 454 lead fields on a Trigger watching a CRM Task, and this
codebase's own rule is that the offer is a subset of the gate. Every subject but `CRM Lead` was blind,
and a subject only exists at all if it resolves to a lead (`subjects.SUBJECTS`), so "the record and its
lead" is well defined for all five.

LAZY, because most triggers never name the lead. The record is offered as a loader and `Values._record`
pulls it at most once, on first reference — so a predicate that reads only the firing document costs
exactly what it cost before.
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import context as ctx_build
from tatva_connect.workflow_engine.tests import fixtures as fx


def _task_on(lead, title="Trigger probe"):
	return frappe.get_doc({
		"doctype": "CRM Task", "title": title, "status": "Backlog",
		"reference_doctype": "CRM Lead", "reference_docname": lead,
	}).insert(ignore_permissions=True)


class TestTheTriggerSpeaksTheSameVocabularyAsTheRoute(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.lead = fx.make_lead(first_name="ZZ Trigger Probe")

	def test_a_lead_field_is_declared_at_a_task_trigger(self):
		"""The DECLARATION half — what `rules._rule_match` checks a rule's field against."""
		declared = ctx_build.field_types_for("CRM Task", "CRM Lead")

		self.assertIn("crm_lead.first_name", declared)
		self.assertIn("crm_task.status", declared)

	def test_a_lead_field_is_readable_at_a_task_trigger(self):
		"""The VALUES half — the context the same rule is then evaluated against."""
		task = _task_on(self.lead.name)

		context = ctx_build.context_for(task, {}, lead=self.lead)

		self.assertIn("crm_lead.first_name", context)
		# Read off the record: the controller rewrites some text fields per grain.
		self.assertEqual(context.get("crm_lead.first_name"), self.lead.first_name)
		self.assertEqual(context.get("crm_task.status"), "Backlog")

	def test_the_lead_is_not_loaded_when_nothing_names_it(self):
		"""Lazy: a trigger that reads only its own record pays nothing for the offer."""
		task = _task_on(self.lead.name)
		pulled = []

		context = ctx_build.context_for(task, {}, lead=self.lead)
		context._loaders["crm_lead"] = lambda: pulled.append(1) or {}

		context.get("crm_task.status")
		self.assertEqual(pulled, [], "the lead was loaded for a predicate that never named it")

		context.get("crm_lead.first_name")
		self.assertEqual(pulled, [1], "the lead should load once, on first reference")

	def test_a_lead_subject_is_not_offered_twice(self):
		"""When the subject IS the lead the in-memory mid-save doc must stay the only answer."""
		context = ctx_build.context_for(self.lead, {}, lead=self.lead)

		self.assertEqual(context.get("crm_lead.first_name"), self.lead.first_name)
		self.assertNotIn("crm_lead", context._loaders)


class TestItRunsTheWorkflowEndToEnd(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fx.arm_engine(True, cls)
		cls.wanted = fx.make_lead(first_name="ZZ Wanted")
		cls.other = fx.make_lead(first_name="ZZ Other")
		cls.workflow = fx.make_workflow(
			f"WF-TRIGGER-LEAD-{frappe.generate_hash(length=6)}",
			[
				# `name`, not a text field: the lead controller rewrites some of those per grain.
				fx.trigger(to="end", subject_doctype="CRM Task", event="Created", predicate={
					"type": "rule", "field": "crm_lead.name", "operator": "is", "value": cls.wanted.name,
				}),
				fx.node("end", "Terminal"),
			],
		)
		cls.addClassCleanup(fx.purge, cls.workflow.name)

	def _journeys(self, lead):
		return frappe.get_all(
			"CRM Workflow Journey", filters={"workflow": self.workflow.name, "subject_name": lead}, pluck="name"
		)

	def test_a_task_on_the_qualifying_lead_starts_the_workflow(self):
		_task_on(self.wanted.name, "qualifies")

		self.assertTrue(self._journeys(self.wanted.name), "the trigger could not read the lead it qualified on")

	def test_a_task_on_any_other_lead_does_not(self):
		"""Proves it EVALUATES rather than passing everything — the failure mode a blind gate would hide."""
		_task_on(self.other.name, "does not qualify")

		self.assertFalse(self._journeys(self.other.name))
