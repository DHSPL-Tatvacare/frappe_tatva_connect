"""Override of frappe's `Notification` rule: its email and bell stay quiet for work a batch that ticked Don't Notify Assignees covers (notifications/batch.py); registered via `override_doctype_class`."""
from frappe.email.doctype.notification.notification import Notification

from tatva_connect.notifications import batch


class QuietNotification(Notification):
	def send_an_email(self, doc, context, template_content=None):
		if not batch.silent():
			super().send_an_email(doc, context, template_content)

	def create_system_notification(self, doc, context, template_content=None):
		if not batch.silent():
			super().create_system_notification(doc, context, template_content)
