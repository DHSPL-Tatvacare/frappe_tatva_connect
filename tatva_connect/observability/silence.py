"""Email an operator when a watched partner API stops sending leads — the one thing the request log cannot say for itself, because silence writes no row."""
import frappe
from frappe.utils import add_to_date, cint, now_datetime, split_emails

from tatva_connect import automation

SWITCH = "Notify::Partner::silence"
SETTINGS = "CRM Partner API Settings"
CONTRACT = "CRM Lead API Mapping"
LOG = "tabCRM API Request Log"
CREATE_ENDPOINTS = ("lead_create", "lead_create_bulk")
DEFAULT_HOURS = 8
MIN_HOURS = 1  # the read floor; the field is `non_negative` and blank falls back to DEFAULT_HOURS, so nothing below this can reach the sweep
INTERVAL_MINUTES = 60  # MUST equal the cadence hooks.py registers `sweep` at — it is the window width, so a mismatch either double-tells or misses; pinned by test_partner_silence


def sweep():
	"""Hourly: one email per watched contract that has just gone quiet for its configured period."""
	if not automation.is_enabled(SWITCH):
		return
	recipients = split_emails(frappe.db.get_single_value(SETTINGS, "silence_alert_recipients") or "")
	if not recipients:
		return
	watched = frappe.get_all(
		CONTRACT, filters={"enabled": 1, "alert_on_silence": 1}, fields=["name", "partner_user"]
	)
	if not watched:
		return

	hours = max(MIN_HOURS, cint(frappe.db.get_single_value(SETTINGS, "silence_alert_hours")) or DEFAULT_HOURS)
	quiet_since = add_to_date(now_datetime(), hours=-hours)
	contract_of = {row.partner_user: row.name for row in watched if row.partner_user}
	for user, last_call in _crossed(list(contract_of), quiet_since):
		_notify(contract_of[user], user, last_call, hours, recipients)


def _crossed(users, quiet_since):
	"""(partner_user, last_call) for each whose newest lead call lands in this pass's window — one pass wide, so a contract is told on exactly one sweep and needs no stamp to remember it."""
	if not users:
		return []
	rows = frappe.db.sql(
		f"""
		SELECT source, MAX(request_time) AS last_call
		FROM `{LOG}`
		WHERE channel = 'Partner API'
		  AND status_code < 400
		  AND endpoint IN %(endpoints)s
		  AND source IN %(users)s
		  AND request_time >= %(floor)s
		GROUP BY source
		HAVING last_call <= %(quiet_since)s
		""",  # sqli-ok: table name is a module constant; every value is bound via %()s
		{
			"endpoints": CREATE_ENDPOINTS,
			"users": tuple(users),
			# The floor is what bounds the read: one interval before the threshold, served by the request_time index. A contract quiet for longer has no row here and was told on the sweep it crossed.
			"floor": add_to_date(quiet_since, minutes=-INTERVAL_MINUTES),
			"quiet_since": quiet_since,
		},
		as_dict=True,
	)
	return [(r.source, r.last_call) for r in rows]


def _notify(contract, user, last_call, hours, recipients):
	"""Queue one email, through the site's default outgoing account like every other mail this app sends."""
	frappe.sendmail(
		recipients=recipients,
		subject=frappe._("Partner API quiet: {0} has sent no leads for {1}h").format(contract, hours),
		message=frappe._(
			"<p><b>{0}</b> has sent no leads for {1} hours.</p>"
			"<p>Last lead call: {2}<br>API user: {3}</p>"
			"<p>Sent once per quiet spell. It reports again only after this contract sends a lead and "
			"then falls silent for {1} hours once more.</p>"
		).format(contract, hours, last_call, user),
		reference_doctype=CONTRACT,
		reference_name=contract,
	)
