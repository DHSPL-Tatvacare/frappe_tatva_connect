# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

import re

import frappe
from frappe import _
from frappe.model.document import Document


class CRMEnrolmentSubmission(Document):
	def validate(self):
		"""Server-side guards that hold on EVERY save path (web form, API, desk) — the
		web form's client rules are advisory only. Fail-closed: a bad value is rejected,
		never silently stored. The picked City/Hospital/Doctor must sit inside the form's
		grain, so a tampered submission can't smuggle an out-of-scope reference. This
		mirrors the scoped lookups in taxonomy.lookups (same scope, an existence check —
		not a second filter)."""
		self._check_phone()
		self._check_in_scope()

	def _check_phone(self):
		# Indian mobile: 10 digits, or 12 with the 91 country code. Canonicalisation to
		# +E.164 happens downstream (the lead brain + dedup); here we only reject garbage.
		digits = re.sub(r"\D", "", self.phone or "")
		if len(digits) not in (10, 12):
			frappe.throw(_("Enter a valid 10-digit mobile number."), title=_("Invalid phone"))

	def _check_in_scope(self):
		# City must belong to the picked State (the State -> City cascade).
		if self.city and self.state and not frappe.db.exists(
			"CRM City", {"name": self.city, "state": self.state}
		):
			frappe.throw(_("Selected city is not in {0}.").format(self.state), title=_("Invalid city"))

		if not self.intake_form:
			return  # no linked form => no grain to validate against
		cfg = frappe.get_cached_doc("CRM Intake Form", self.intake_form)

		# Hospital / Doctor anti-spoof, enforced ONLY where the master actually carries the
		# data: a grain-tagged hospital must match the form's grain; a doctor linked to a
		# hospital must be at the picked one. Untagged masters (today's seed has NULL grain /
		# NULL hospital FK) are a no-op here — we never reject a legitimately-picked reference
		# the catalog hasn't scoped yet.
		if self.hospital and not self.hospital_not_listed:
			h = frappe.db.get_value(
				"CRM Hospital", self.hospital, ["vertical", "group", "program"], as_dict=True
			)
			if h and all([h.vertical, h.group, h.program]) and (
				h.vertical != cfg.custom_vertical
				or h.group != cfg.custom_group
				or h.program != cfg.custom_current_program
			):
				frappe.throw(
					_("Selected hospital is not available for this programme."),
					title=_("Invalid hospital"),
				)

		if self.doctor and not self.doctor_not_listed and self.hospital:
			doc_hospital = frappe.db.get_value("CRM Doctor", self.doctor, "hospital")
			if doc_hospital and doc_hospital != self.hospital:
				frappe.throw(
					_("Selected doctor is not at the chosen hospital."), title=_("Invalid doctor")
				)
