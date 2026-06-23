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

		# Hospital must be inside the form's grain. `all(grain.values())` mirrors the
		# lookup, which returns nothing when the grain is incomplete (so no valid pick).
		grain = {
			"vertical": cfg.custom_vertical,
			"group": cfg.custom_group,
			"program": cfg.custom_current_program,
		}
		if self.hospital and not self.hospital_not_listed and all(grain.values()):
			if not frappe.db.exists("CRM Hospital", {"name": self.hospital, **grain}):
				frappe.throw(
					_("Selected hospital is not available for this programme."),
					title=_("Invalid hospital"),
				)

		# Doctor must practise at the picked Hospital (the Doctor -> Hospital FK).
		if (
			self.doctor
			and not self.doctor_not_listed
			and self.hospital
			and not frappe.db.exists("CRM Doctor", {"name": self.doctor, "hospital": self.hospital})
		):
			frappe.throw(
				_("Selected doctor is not at the chosen hospital."), title=_("Invalid doctor")
			)
