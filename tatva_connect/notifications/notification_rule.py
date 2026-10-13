"""Override of frappe's `Notification` rule: the email and bell channels stay quiet for the work a Live import ticked; registered via `override_doctype_class`."""
from frappe.email.doctype.notification.notification import Notification

from tatva_connect.notifications import quiet


class QuietNotification(Notification):
	def send_an_email(self, doc, context, template_content=None):
		if not quiet.muted(quiet.EMAIL):
			super().send_an_email(doc, context, template_content)

	def create_system_notification(self, doc, context, template_content=None):
		if not quiet.bell_muted():
			super().create_system_notification(doc, context, template_content)
