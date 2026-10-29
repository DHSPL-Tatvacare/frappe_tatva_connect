# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The SPA's check-in door: the caller's own status, its once-per-login prompt and notice. Every rule is `lead/checkin.py`'s."""

import frappe
from frappe.sessions import get_expiry_in_seconds
from frappe.utils import get_fullname, nowdate

from tatva_connect.lead import checkin

# Answered for the day, not the login: a session outlives a shift by days, and tomorrow's shift asks again.
_ANSWERED = "tatva_connect:checkin:answered:{sid}:{day}"
_NOTICED = "tatva_connect:checkin:noticed:{sid}"


@frappe.whitelist()
def get_status():
	"""The caller's status card; `prompt` holds until the caller answers it today, `notice` is told once per login, and neither while `enabled` is false."""
	return _card(frappe.session.user)


@frappe.whitelist(methods=["POST"])
def set_status(status):
	"""Set the caller's own status; the row is inserted under their own permission, so Frappe decides whether they may."""
	checkin.record(frappe.session.user, status)
	mark_answered()
	return _card(frappe.session.user)


@frappe.whitelist(methods=["POST"])
def dismiss_prompt():
	""""Not now": not asked again today."""
	mark_answered()
	return {"ok": True}


def mark_answered():
	"""Any status the caller picks for themselves, in either app, or "Not now", answers today's ask for this login."""
	_mark(_ANSWERED, 1)


def _card(user):
	row = checkin.latest(user)
	status = row.status if row else None
	changed_by = row.owner if row and row.owner != user else None
	on_shift = checkin.on_shift(user)
	enabled = checkin.uses_checkin(user)
	# Nothing is asked or marked for a user whose business lines do not use check-in.
	# Asked on every load until answered: a tab closed unanswered asks again in the next one.
	prompt = enabled and not _seen(_ANSWERED) and checkin.wants_check_in(user)
	# A change the rep did not make (a manager's, or the shift-end check-out) is told once, keyed on the row, so a second one in the same session is told too.
	notice = bool(enabled and row and row.source in (checkin.MANAGER, checkin.SHIFT_END) and _seen(_NOTICED) != row.name)
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
		# Why the pool gives no lead now (the draw's own reason, none when it would), with the shift's end and the next opening.
		"lead_flow": checkin.lead_flow(user) if enabled else None,
	}


def _seen(key):
	return frappe.cache.get_value(key.format(sid=frappe.session.sid, day=nowdate()), expires=True)


def _mark(key, value):
	# Kept as long as an idle login lives; the day in the answered key is what ends it.
	frappe.cache.set_value(key.format(sid=frappe.session.sid, day=nowdate()), value, expires_in_sec=get_expiry_in_seconds())
