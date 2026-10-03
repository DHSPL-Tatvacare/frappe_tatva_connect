# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The SPA's check-in door: the caller's own status, its once-per-login prompt and notice. Every rule is `lead/checkin.py`'s."""

import frappe
from frappe.sessions import get_expiry_in_seconds
from frappe.utils import get_fullname

from tatva_connect.lead import checkin

_PROMPTED = "tatva_connect:checkin:prompted:{sid}"
_NOTICED = "tatva_connect:checkin:noticed:{sid}"


@frappe.whitelist()
def get_status():
	"""The caller's status card; `prompt` and `notice` are each answered true once per login session, and never while `enabled` is false."""
	return _card(frappe.session.user)


@frappe.whitelist(methods=["POST"])
def set_status(status):
	"""Set the caller's own status; the row is inserted under their own permission, so Frappe decides whether they may."""
	checkin.record(frappe.session.user, status)
	return _card(frappe.session.user)


@frappe.whitelist(methods=["POST"])
def dismiss_prompt():
	""""Not now": this login session is not asked again."""
	_mark(_PROMPTED, 1)
	return {"ok": True}


def _card(user):
	row = checkin.latest(user)
	status = row.status if row else None
	changed_by = row.owner if row and row.owner != user else None
	on_shift = checkin.on_shift(user)
	enabled = checkin.uses_checkin(user)
	# Nothing is asked or marked for a user whose business lines do not use check-in.
	prompt = enabled and not _seen(_PROMPTED) and checkin.wants_check_in(user)
	if prompt:
		_mark(_PROMPTED, 1)
	# A manager's change is told once, keyed on the row, so a second change in the same session is told too.
	notice = bool(enabled and row and row.source == checkin.MANAGER and _seen(_NOTICED) != row.name)
	if notice:
		_mark(_NOTICED, row.name)
	return {
		"enabled": enabled,
		"status": status,
		"since": row.creation if row else None,
		"source": row.source if row else None,
		"changed_by": changed_by,
		"changed_by_name": get_fullname(changed_by) if changed_by else None,
		"on_shift": on_shift,
		"prompt": prompt,
		"notice": notice,
	}


def _seen(key):
	return frappe.cache.get_value(key.format(sid=frappe.session.sid), expires=True)


def _mark(key, value):
	# Kept as long as an idle login lives, so a reload or a new tab in it never asks again.
	frappe.cache.set_value(key.format(sid=frappe.session.sid), value, expires_in_sec=get_expiry_in_seconds())
