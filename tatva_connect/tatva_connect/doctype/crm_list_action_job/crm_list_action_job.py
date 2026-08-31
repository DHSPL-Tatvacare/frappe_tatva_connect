# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""One bulk list-view action (20+ rows), drained by a worker.

SHAPE BORROWED FROM `CRM Export Job`, NOT INVENTED — same three hooks, same reasoning: `before_insert`
sets Queued, `after_insert` enqueues on the long lane, `on_trash` stops a job still owed.

LONG, NOT SHORT. `BULK_TIMEOUT` is 20 minutes, but the short lane this codebase otherwise uses for
per-item work (search/index.py, storage/file_screening.py, notifications/dispatch.py) expects jobs to
finish in roughly 300s — a worst-case 500-row cascade-delete could occupy that shared lane for the
full 20 minutes, so this goes on the long lane instead.
"""
from contextlib import suppress

import frappe
from frappe.model.document import Document
from frappe.utils.background_jobs import enqueue

BULK_TIMEOUT = 1200


class CRMListActionJob(Document):
	def before_insert(self):
		self.status = "Queued"

	def after_insert(self):
		enqueue(
			"tatva_connect.bulk_actions.run",
			queue="long",
			timeout=BULK_TIMEOUT,
			job=self.name,
			enqueue_after_commit=True,
		)

	def on_trash(self):
		"""Stop a job that is still owed. A finished job has no job left to stop."""
		if self.status not in ("Queued", "Started") or not self.job_id:
			return
		with suppress(Exception):
			rq_job = frappe.get_doc("RQ Job", self.job_id)
			rq_job.stop_job() if self.status == "Started" else rq_job.delete()
