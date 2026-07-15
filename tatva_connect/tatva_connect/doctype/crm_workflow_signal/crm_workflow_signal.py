# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class CRMWorkflowSignal(Document):
	"""The durable signal inbox. A delivery is one Pending row (never a touch on an Instance); a
	Wait[Until Event] consumes the first matching row and marks it Consumed. This controller holds no
	logic - the interpreter's `_consume_signal` owns the Pending->Consumed transition under a write lock."""
