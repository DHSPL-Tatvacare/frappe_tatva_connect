"""The Notification Types we ship (fixtures/notification_type.json), named once; no imports, so hooks.py reads it too."""
# crm writes WhatsApp; dispatch writes the rest. Frappe owns Assignment, Mention and Share.
WHATSAPP = "WhatsApp"
MISSED_CALL, TASK_DUE_SOON, TASK_OVERDUE, LEAD_MOVING_SOON, LEAD_MOVED, WORK_WAITING = TYPES = (
	"Missed Call", "Task Due Soon", "Task Overdue", "Lead Moving Soon", "Lead Moved", "Work Waiting")
SHIPPED = (WHATSAPP, *TYPES)
