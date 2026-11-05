"""Every Tatva Activity Log row saved before the headline existed gets its headline, back-at time and idle minutes, read from its own columns."""

import frappe

from tatva_connect.tatva_connect.doctype.tatva_activity_log.tatva_activity_log import describe

_DOCTYPE = "Tatva Activity Log"


def execute():
	rows = frappe.get_all(
		_DOCTYPE, filters={"headline": ["is", "not set"]},
		fields=["name", "event", "status", "source", "owner", "creation", "last_active_at"],
	)
	for row in rows:
		# The row is a log and never re-saved; its derived columns are written in place, its modified time kept.
		frappe.db.set_value(_DOCTYPE, row.name, describe(row), update_modified=False)
