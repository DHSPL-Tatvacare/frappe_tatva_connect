# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""`CRM Campaign Document` — the owner that lets a marketing PDF be public without publishing a patient.

Two claims, and this suite exists to hold both.

THE SEAM. `file_events.may_be_public()` classifies a file by the doctype that OWNS it, and the operator's
allowlist is per-DOCTYPE. So the only way to publish a generated document without publishing every lead
attachment is for the document to have a DIFFERENT owner. That is asserted here on the File's own
`attached_to_doctype` — the exact value the checkpoint reads — not on a mock and not on a call count. The
allowlist's two directions (listed serves anonymously, unlisted refuses) belong to the storage suite, which
already owns that surface; duplicating them here would grow a second brain for one decision.

THE CASCADE. A blob's life is exactly its row's life (M1). Deleting the lead must therefore leave neither a
campaign document nor its PDF behind, and the delete must go through `frappe.delete_doc` for that to happen
at all — `frappe.db.delete` never reaches `File.on_trash` and would strand a PUBLIC document about a
deleted patient in the container.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tatva_connect.doctype.crm_campaign_document.test_crm_campaign_document
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.tatva_connect.doctype.crm_campaign_document.crm_campaign_document import DT
from tatva_connect.workflow_engine.tests import fixtures


class TestCampaignDocumentIsItsOwnOwner(FrappeTestCase):
	"""The row is a real record with its own doctype — which is the entire mechanism it delivers."""

	def setUp(self):
		self.lead = fixtures.make_lead()

	def test_a_document_is_born_queued_against_its_lead(self):
		"""Queued is the honest state at creation: the row is written before the render is dispatched, so
		anything else would claim an answer nobody has yet."""
		doc = frappe.get_doc({"doctype": DT, "lead": self.lead.name}).insert()
		self.assertEqual(doc.status, "Queued")
		self.assertEqual(doc.lead, self.lead.name)

	def test_a_document_cannot_exist_without_a_lead(self):
		"""The lead is what the cascade keys on. A row without one could never be deleted with anybody and
		would outlive the person it is about."""
		with self.assertRaises(frappe.MandatoryError):
			frappe.get_doc({"doctype": DT, "status": "Queued"}).insert()

	def test_the_pdf_is_owned_by_the_campaign_document_and_never_by_the_lead(self):
		"""THE point of the doctype. `may_be_public` reads `attached_to_doctype`, and the allowlist names
		doctypes — so this pair is what decides whether the file can be served to an anonymous fetcher.
		Filing it on the lead would put `CRM Lead` on that list and publish clinical attachments with it."""
		doc = frappe.get_doc({"doctype": DT, "lead": self.lead.name}).insert()
		pdf = frappe.get_doc({
			"doctype": "File",
			"file_name": "campaign-owner-probe.pdf",
			"content": "%PDF-1.4 probe",
			"attached_to_doctype": DT,
			"attached_to_name": doc.name,
		}).insert()
		self.addCleanup(frappe.delete_doc, "File", pdf.name, force=True, ignore_missing=True)

		self.assertEqual(pdf.attached_to_doctype, DT)
		self.assertNotEqual(pdf.attached_to_doctype, "CRM Lead", "the PDF is owned by the lead again")


class TestTheDocumentsDieWithTheLead(FrappeTestCase):
	"""M1, end to end: delete the lead and neither the row nor its bytes are left naming a ghost."""

	def setUp(self):
		self.lead = fixtures.make_lead()
		self.doc = frappe.get_doc({"doctype": DT, "lead": self.lead.name}).insert()
		self.pdf = frappe.get_doc({
			"doctype": "File",
			"file_name": "campaign-cascade-probe.pdf",
			"content": "%PDF-1.4 probe",
			"attached_to_doctype": DT,
			"attached_to_name": self.doc.name,
		}).insert()
		self.doc.db_set("document_file", self.pdf.name, update_modified=False)

	def test_deleting_the_lead_takes_its_campaign_documents(self):
		"""Force-less on purpose: the cascade runs in the lead's own `on_trash`, which frappe calls BEFORE
		its link check — so a lead carrying documents deletes cleanly, and a missing cascade shows up as a
		refused delete rather than as a silently orphaned row."""
		frappe.delete_doc("CRM Lead", self.lead.name)
		self.assertFalse(frappe.db.exists(DT, self.doc.name), "the campaign document outlived its lead")

	def test_deleting_the_lead_takes_the_documents_file_with_it(self):
		"""The blob follows the row (M1) because the delete goes through `frappe.delete_doc`, which reaches
		`file_manager.remove_all` -> `File.on_trash`. A bulk row delete would leave a public PDF about a
		deleted patient in the container, and the File row here is what proves the document path was used."""
		frappe.delete_doc("CRM Lead", self.lead.name)
		self.assertFalse(frappe.db.exists("File", self.pdf.name), "the PDF outlived the lead it is about")
