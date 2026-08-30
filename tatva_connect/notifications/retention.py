"""Tray retention — the only thing that ever removes a `CRM Notification` row.

Nothing else does. A row is written when a rep is told something and then lives for ever, so the table
only grows and every tray read grows with it. This trims the ones that have served their purpose.

An UNREAD row is never purged, however old: it is someone's outstanding work item, and age is not consent.

DORMANT BY DESIGN: gated by `Notifications::Tray::retention`, which ships OFF. Wiring the scheduler entry
changes nothing until an operator turns the switch on.
"""
import frappe
from frappe.utils import add_to_date, now_datetime

from tatva_connect import automation

SWITCH_RETENTION = "Notifications::Tray::retention"
DOCTYPE = "CRM Notification"
DEFAULT_DAYS = 90

# The youngest retention a caller may ask for. `days=0` is the whole hole this guards: it puts the floor at NOW, which matches every read row on the site and empties the tray for everyone.
MIN_DAYS = 7

# Rows per statement. An unbounded DELETE on a table nobody has ever trimmed holds locks for as long as it runs; this bounds every statement and commits between them.
BATCH = 5000


def purge_read_notifications(days: int = DEFAULT_DAYS) -> dict:
	"""Delete read tray rows older than `days`, in bounded batches. Returns a summary."""
	if not automation.is_enabled(SWITCH_RETENTION):
		return {"ok": False, "reason": f"{SWITCH_RETENTION} disabled"}

	days = abs(int(days))
	if days < MIN_DAYS:
		return {"ok": False, "reason": f"retention floor is {MIN_DAYS} days; refused {days}"}

	floor = add_to_date(now_datetime(), days=-days)
	deleted = 0
	while True:
		# The names are read first so the DELETE is keyed on the primary key and can never widen past this page.
		names = frappe.get_all(
			DOCTYPE, filters={"read": 1, "creation": ["<", floor]}, pluck="name", limit=BATCH
		)
		if not names:
			break
		frappe.db.delete(DOCTYPE, {"name": ["in", names]})
		frappe.db.commit()
		deleted += len(names)
	return {"ok": True, "deleted": deleted, "before": str(floor)}
