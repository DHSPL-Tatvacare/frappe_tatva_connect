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
from contextlib import suppress

import frappe
from frappe.model.document import Document
from frappe.utils.background_jobs import enqueue

# The long lane already runs everywhere; a producer caps its own rows, so this only outlasts the drain.
EXPORT_TIMEOUT = 1500


class CRMExportJob(Document):
	def before_insert(self):
		self.status = "Queued"

	def after_insert(self):
		enqueue(
			"tatva_connect.exports.run",
			queue="long",
			timeout=EXPORT_TIMEOUT,
			job=self.name,
			enqueue_after_commit=True,
		)

	def on_trash(self):
		"""Stop a job that is still owed. A finished export has no job left to stop."""
		if self.status not in ("Queued", "Started") or not self.job_id:
			return
		with suppress(Exception):
			rq_job = frappe.get_doc("RQ Job", self.job_id)
			rq_job.stop_job() if self.status == "Started" else rq_job.delete()
