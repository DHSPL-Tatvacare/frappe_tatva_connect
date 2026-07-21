# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from tatva_connect import automation
from tatva_connect.storage import file_screening


class CRMFileScreeningSettings(Document):
	def validate(self):
		"""Refuse config the screener cannot read, here rather than on the patient's upload.

		Both fields are free text the screener parses on every screened upload, so a typo does not sit
		harmlessly on a form — it either refuses every file or waves every file through. The check is the
		screener's OWN parser and the screener's OWN shipped list, never a second copy of either."""
		self._validate_signatures()
		self._validate_blocking_verdicts()

	def on_update(self):
		"""Re-register the scan log so a changed retention takes effect on save, not on the next toggle.

		Retention is executed by Log Settings, and the row it reads is written by the screening toggle's
		activator. Saving a new number here without re-running it would leave the form saying one thing
		and the cleanup doing another. The activator is passed the toggle's CURRENT state, so this never
		arms anything an operator has not already turned on."""
		file_screening.apply_scan_logging(automation.is_enabled(file_screening.TOGGLE))

	def _validate_signatures(self):
		if not self.type_signatures:
			return
		try:
			file_screening.parse_signatures(self.type_signatures)
		except ValueError:
			frappe.throw(
				_("Type Signatures must read as TYPE = HEX, HEX — one type per line, hex pairs only."),
				title=_("Unreadable signature table"),
			)

	def _validate_blocking_verdicts(self):
		"""The blockable verdicts are exactly the shipped list — the facts about a FILE. Scanner Unavailable
		is excluded on purpose: it is decided by its own field, and naming it here would be a second decider."""
		if not self.blocking_verdicts:
			return
		known = set(file_screening.DEFAULTS["blocking_verdicts"].splitlines())
		lines = [line.strip() for line in self.blocking_verdicts.splitlines() if line.strip()]
		unknown = [line for line in lines if line not in known]
		if unknown:
			frappe.throw(
				_("Not verdicts a file can be blocked on: {0}. Allowed: {1}.").format(
					", ".join(unknown), ", ".join(sorted(known))
				),
				title=_("Unknown verdict"),
			)
