# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""One subject's journey through a frozen workflow version. Durable: it parks, resumes and retries, and it binds to a VERSION so an edit to the workflow can never re-route a journey already under way."""
from frappe.model.document import Document


class CRMWorkflowJourney(Document):
	pass
