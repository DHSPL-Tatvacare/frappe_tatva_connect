# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""One inbound fact a parked Run may be waiting for. Written by `workflow_engine.signals`, consumed exactly once by the interpreter; buffered so an event that arrives BEFORE its Wait is not lost."""
from frappe.model.document import Document


class CRMWorkflowSignal(Document):
	pass
