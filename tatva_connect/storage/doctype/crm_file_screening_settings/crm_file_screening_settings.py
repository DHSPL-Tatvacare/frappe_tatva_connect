# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

from frappe.model.document import Document

from tatva_connect import automation
from tatva_connect.storage import file_screening


class CRMFileScreeningSettings(Document):
	def on_update(self):
		"""Re-register the scan log so a changed retention takes effect on save, not on the next toggle.

		Retention is executed by Log Settings, and the row it reads is written by the screening toggle's
		activator. Saving a new number here without re-running it would leave the form saying one thing
		and the cleanup doing another. The activator is passed the toggle's CURRENT state, so this never
		arms anything an operator has not already turned on."""
		file_screening.apply_scan_logging(automation.is_enabled(file_screening.TOGGLE))
