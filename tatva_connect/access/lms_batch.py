# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A batch is managed by whoever made it — the line `LMS Course` has and `LMS Batch` does not.

Creating and managing a batch are decided by two different systems, and upstream lets them disagree.
CREATE is DocPerm, where `Batch Evaluator` holds create on `LMS Batch`. MANAGE is `can_modify_batch`,
which asks `has_moderator_role() or is_instructor` and consults neither Batch Evaluator nor Course
Creator. So the role that owns the doctype can make a batch it then cannot open: every tab renders
blank, because `get_batch_details` answers `{}` for a caller who is not published-into, enrolled, or
an instructor.

`LMS Course.validate_instructors` already fixes exactly this for courses — new, no instructors, add the
owner. Batches never got the line. This is that line, in the same place upstream put it (the controller,
before the row is judged) rather than a hook that would land after.

Not a permission grant: it writes the `Course Instructor` row that lms's own "who runs this" question
already reads, so the answer comes from lms's mechanism and ours adds no second one.
"""
import frappe
from lms.lms.doctype.lms_batch.lms_batch import LMSBatch


class TatvaLMSBatch(LMSBatch):
	def validate(self):
		self._name_an_instructor()
		super().validate()

	def _name_an_instructor(self):
		"""The creator is always among a new batch's instructors, whoever else they name.

		`LMS Course` only fills the list when it is EMPTY, so naming a colleague there locks the author out
		of their own course. A batch is worse: it has no such line at all. Both leave `can_modify_batch`
		answering False for the one person certain to need it, and every tab renders blank."""
		if not self.is_new():
			return
		owner = self.owner or frappe.session.user
		if any(row.instructor == owner for row in self.get("instructors") or []):
			return
		self.append("instructors", {"instructor": owner})
