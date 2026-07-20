# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""One action's execution. `idempotency_key` is UNIQUE, so a replayed action is refused by the database rather than by each caller remembering to check."""
from frappe.model.document import Document


class CRMWorkflowActionLog(Document):
	pass
