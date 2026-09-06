# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A workflow starts when an email arrives on a patient.

The engine already fired on every doctype through the wildcard front-door; a doctype participates only
because `automation.subjects.SUBJECTS` names it and says how it reaches a `CRM Lead`. Communication was
not named, so an inbound email resolved no subject and the run was dropped fail-closed.

WHICH EMAILS REACH A PATIENT, AND WHICH DO NOT. A reply to one a rep sent from the CRM inherits the
parent's reference (frappe's own receiver), so it arrives filed against the lead and this trigger fires.
A cold email from a stranger references nothing, resolves to no lead, and starts nothing — deliberately.
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.tests.authz.grains import assert_masters_exist
from tatva_connect.workflow_engine.tests import fixtures as fx

_WORKFLOW = "inbound-email-probe"


class TestAnInboundEmailStartsAWorkflow(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		fx.purge(_WORKFLOW)
		fx.arm_engine(True, cls)
		cls.lead = fx.make_lead()
		# Wait-free, so it runs inline and finishes in the same breath — the journey row is the assertion.
		cls.workflow = fx.make_workflow(_WORKFLOW, [
			fx.trigger(to="end", subject_doctype="Communication", event="Created", predicate={
				"type": "rule", "field": "communication.sent_or_received", "operator": "is",
				"value": "Received",
			}),
			fx.node("end", "Terminal"),
		])
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		fx.purge(_WORKFLOW)
		frappe.db.delete("Communication", {"subject": ("like", "Probe%")})
		frappe.delete_doc("CRM Lead", cls.lead.name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def tearDown(self):
		self._clear()

	def _clear(self):
		for run in frappe.get_all(fx.JOURNEY_DT, filters={"workflow": _WORKFLOW}, pluck="name"):
			frappe.db.delete(fx.STEP_LOG_DT, {"journey": run})
		frappe.db.delete(fx.JOURNEY_DT, {"workflow": _WORKFLOW})
		frappe.db.commit()

	def _email(self, on_lead=True, direction="Received", body="Can I get a refill?"):
		"""An email as frappe's receiver files one: the patient is the reference it carries."""
		doc = {
			"doctype": "Communication", "communication_type": "Communication",
			"sent_or_received": direction, "subject": "Probe: re your visit",
			"sender": "asha@example.invalid", "content": body,
		}
		if on_lead:
			doc.update({"reference_doctype": "CRM Lead", "reference_name": self.lead.name})
		return frappe.get_doc(doc).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input

	def _runs(self):
		return frappe.get_all(fx.JOURNEY_DT, filters={"workflow": _WORKFLOW},
		                      fields=["name", "status", "subject_name", "state_json"])

	def test_a_reply_on_a_patient_starts_a_journey_about_that_patient(self):
		"""THE red — Communication was not a subject, so this resolved nothing and ran nothing."""
		self._email()

		runs = self._runs()
		self.assertEqual(len(runs), 1, "an email filed on a patient must start exactly one journey")
		self.assertEqual(runs[0].subject_name, self.lead.name, "the journey is about the patient, not the email")
		self.assertEqual(runs[0].status, "Done")

	def test_the_body_and_the_sender_are_readable(self):
		"""What the feature is FOR. The trigger doc's bucket is stored whole, so a later node reads it."""
		self._email(body="Can I get a refill?")

		state = frappe.parse_json(self._runs()[0].state_json or "{}").get("communication", {})
		self.assertEqual(state.get("content"), "Can I get a refill?")
		self.assertEqual(state.get("sender"), "asha@example.invalid")
		self.assertEqual(state.get("sent_or_received"), "Received")

	def test_an_email_the_rep_sent_does_not_start_it(self):
		"""Without the Received pin every outbound email fires the trigger too — the author's own rule."""
		self._email(direction="Sent")

		self.assertEqual(self._runs(), [], "an outbound email is not an email arriving")

	def test_an_email_on_no_patient_starts_nothing(self):
		"""A cold email resolves to no lead. Fail-closed, silently, and that is the intended answer."""
		self._email(on_lead=False)

		self.assertEqual(self._runs(), [], "an email on nobody must not start a journey")
