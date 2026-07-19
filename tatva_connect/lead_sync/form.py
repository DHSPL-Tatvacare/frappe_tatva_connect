"""Facebook Lead Form override: a question maps to a catalog field_key, and only one this form's grain owns.

`mapped_to_crm_field` is free text on the fork doctype, so nothing structurally stops an operator naming
a field the brain never declared — the answer would then be collected from the patient and silently
dropped at ingestion. The picker (lead_sync/api.py) offers only the contract's ticked keys; this is the
backstop for an API or import write that never opened the form.
"""
import frappe
from crm.lead_syncing.doctype.facebook_lead_form.facebook_lead_form import FacebookLeadForm

from tatva_connect.api.partner import LEAD_IDENTITY, PARENT_SECTION
from tatva_connect.lead_sync.contract import allowed_field_keys, contract_of

IDENTITY_KEY = f"{PARENT_SECTION}:{LEAD_IDENTITY}"


def contract_for_form(form_name: str):
	"""The contract of the source that crawls this form, or None when there is none to judge against.

	`contract_of` is THE resolver — it also refuses a disabled contract, which is why the picker must not
	re-implement it: an unusable contract would otherwise offer keys ingestion will reject on every lead."""
	source = frappe.db.get_value(
		"Lead Sync Source", {"facebook_lead_form": form_name}, ["name", "api_mapping"], as_dict=True
	)
	if not source:
		return None
	try:
		return contract_of(source)
	except frappe.ValidationError:
		return None


class TatvaFacebookLeadForm(FacebookLeadForm):
	def check_mandatory_crm_fields_mapped(self):
		"""Identity, in KEY space. Upstream compares bare fieldnames ("first_name") against this column, which holds catalog field_keys ("lead:first_name") — so it can never match and throws on every edit. What ingestion actually requires is the dedup key."""
		if self.is_new():
			return
		mapped = {(q.mapped_to_crm_field or "").strip() for q in self.questions}
		if IDENTITY_KEY not in mapped:
			frappe.throw(
				frappe._("Map a question to {0} — the lead is deduped on phone, so a form without it cannot create one.").format(
					frappe.bold(IDENTITY_KEY)
				),
				title=frappe._("Phone Mapping Required"),
			)

	def validate(self):
		super().validate()
		self._validate_mapped_keys()

	def _validate_mapped_keys(self):
		"""Every mapped target must be a key this form's grain may write. Skipped while no source points at the form — there is no grain to judge against yet."""
		contract = contract_for_form(self.name)
		if not contract:
			return
		allowed = allowed_field_keys(contract)
		for question in self.questions:
			key = (question.mapped_to_crm_field or "").strip()
			if key and key not in allowed:
				frappe.throw(
					frappe._("'{0}' is not a field this form's grain may write (question '{1}'). Pick from the list — it shows exactly what the contract allows.").format(
						key, question.label or question.key
					),
					title=frappe._("Target Not In This Grain"),
				)
