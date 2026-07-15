# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

from frappe.model.document import Document

_LIVE = ("Running", "Parked")


class CRMWorkflowInstance(Document):
	"""One durable execution per subject-in-workflow. The interpreter advances it and commits at each
	suspension boundary; this controller only maintains `active_key` so the DB unique index can reject a
	second live Instance for the same (workflow, subject). A live execution carries the key; a terminal
	one carries NULL (InnoDB treats multiple NULLs as distinct, so terminal rows never collide)."""

	def before_save(self):
		self.active_key = "{}::{}".format(self.workflow, self.subject_name) if self.status in _LIVE else None
