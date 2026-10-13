# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""One request to download a listing, drained by a worker.

THE ROW IS THE FILE'S OWNER (M1). The file is born attached to this record, so the blob's life is this
row's life and no export is ever an orphan — the rule `storage/file_override.py` states for every other
file here. It is also what makes the download permissioned twice over: the File's own owner is the person
who asked, and frappe's `File.has_permission` falls through to this record, which is `if_owner`. One
rep's export is unreachable to another even though both are private files on the same disk.

SHAPE BORROWED, NOT INVENTED. `before_insert` sets Queued, `after_insert` enqueues on the long lane,
`on_trash` stops a job still owed — the same three hooks frappe's own `Prepared Report` uses for the same
job, so there is one pattern on this bench and not two.

WHY NOT FRAPPE'S OWN BACKGROUND EXPORT. `reportview.export_query` already has an `export_in_background`
branch, and it delivers by EMAILING the file (`run_report_view_export_job` -> `send_report_email`). For
this product that is the wrong door twice: the SPA wants the file back in the tab that asked, and a
patient-adjacent export should not leave the building as a mail attachment to satisfy a timeout.
"""

import frappe
from frappe.model.document import Document
from frappe.utils.background_jobs import enqueue

# The long lane already runs everywhere; a producer caps its own rows, so this only outlasts the drain.
EXPORT_TIMEOUT = 1500


class CRMExportJob(Document):
	def before_insert(self):
		self.status = "Queued"
		self.seen = 0  # news to its owner until opened in the Bulk Actions panel

	def after_insert(self):
		enqueue(
			"tatva_connect.exports.run",
			queue="long",
			timeout=EXPORT_TIMEOUT,
			job=self.name,
			enqueue_after_commit=True,
		)

	def on_trash(self):
		"""Stop a job that is still owed, through the one stop the panel's Abort uses."""
		from tatva_connect import job_feed

		job_feed.halt(self)
