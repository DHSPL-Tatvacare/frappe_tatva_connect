# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Notification health as NUMBERS ONLY: the one brain the Notifications cards, charts and type report read.

Every notice is one `Notification Log` row: the bell is the row, push stamps `push_state` on it, and
email goes out only to a user who ticked that type. Each figure is read off those rows and the users'
`Notification Settings`; nothing new is stored. Every read is bounded by `creation`, so it rides the
table's index, and the 7-day totals are one aggregate query shared by every card for a minute.

Every public entry is `frappe.only_for("System Manager")`, the gate the Observability workspaces carry.
"""

import frappe
from frappe.query_builder import Case
from frappe.query_builder.functions import Count, Date, Sum
from frappe.utils import add_days, add_to_date, cint, flt, getdate, now_datetime, today

from tatva_connect.notifications import api, push, sender

LOG, SUBSCRIPTION, PREFERENCE = "Notification Log", "CRM Push Subscription", "Notification Type Preference"
EMAIL_FIELD, PUSH_FIELD = api.EMAIL_TYPES_FIELD, api.PUSH_TYPES_FIELD
WINDOW_DAYS, TREND_DAYS, STALE_DAYS = 7, 30, 30
_CACHE_KEY, _CACHE_TTL = "tatva:notifications_health:{}", 60
NOT_REACHED = "Not reached"


def _unstamped(log):
	"""A row no pass pushed or skipped: blank, Pending or Expired."""
	return log.push_state.isnull() | log.push_state.isin(["", push.PENDING, push.EXPIRED])


def _since(days):
	return add_days(getdate(today()), 1 - days)


def window_totals(days=WINDOW_DAYS) -> dict:
	"""Written, read and each push outcome over the last `days`, in one aggregate query, cached a minute."""
	key = _CACHE_KEY.format(days)
	cached = frappe.cache.get_value(key)
	if cached:
		return cached
	log = frappe.qb.DocType(LOG)
	reached_by = add_to_date(now_datetime(), seconds=-sender.STALE_SECONDS)
	row = (
		frappe.qb.from_(log)
		.select(
			Count("*").as_("written"),
			Sum(log.read).as_("read"),
			Sum(Case().when(log.push_state == push.SENT, 1).else_(0)).as_("push_sent"),
			Sum(Case().when(log.push_state.isin(push.SKIPPED), 1).else_(0)).as_("push_skipped"),
			Sum(Case().when(_unstamped(log) & (log.creation < reached_by), 1).else_(0)).as_("push_not_reached"),
		)
		.where(log.creation >= _since(days))
	).run(as_dict=True)[0]
	totals = {k: cint(v) for k, v in row.items()}
	totals["read_rate"] = round(100 * totals["read"] / totals["written"], 1) if totals["written"] else 0
	frappe.cache.set_value(key, totals, expires_in_sec=_CACHE_TTL)
	return totals


def _count_since(days, **filters):
	return frappe.db.count(LOG, {"creation": [">=", _since(days)], **filters})


def _push_not_reached_24h() -> int:
	"""Rows from the last day that push never stamped, though the drain's window for them has passed."""
	log, at = frappe.qb.DocType(LOG), now_datetime()
	return cint((
		frappe.qb.from_(log).select(Count("*"))
		.where((log.creation >= add_days(at, -1)) & (log.creation < add_to_date(at, seconds=-sender.STALE_SECONDS)) & _unstamped(log))
	).run()[0][0])


def _stale_devices() -> int:
	cutoff = add_days(now_datetime(), -STALE_DAYS)
	return frappe.db.count(SUBSCRIPTION, {"last_seen": ["<", cutoff]}) + frappe.db.count(SUBSCRIPTION, {"last_seen": ["is", "not set"]})


# measure: how the card reads it. The 7-day ones share window_totals; the rest are one indexed count each.
_MEASURES = {
	"push_not_reached_24h": (_push_not_reached_24h, "Int"),
	"push_devices_stale": (_stale_devices, "Int"),
	"unread_30d": (lambda: _count_since(TREND_DAYS, read=0), "Int"),
	"push_not_reached_7d": (lambda: window_totals()["push_not_reached"], "Int"),
	"push_skipped_7d": (lambda: window_totals()["push_skipped"], "Int"),
	"written_7d": (lambda: window_totals()["written"], "Int"),
	"read_7d": (lambda: window_totals()["read"], "Int"),
	"read_rate_7d": (lambda: window_totals()["read_rate"], "Percent"),
	"push_sent_7d": (lambda: window_totals()["push_sent"], "Int"),
}


@frappe.whitelist()
def card(filters=None) -> dict:
	"""A Notifications number card: the measure named in its `filters_json`."""
	frappe.only_for("System Manager")
	measure = (frappe.parse_json(filters) or {}).get("measure")
	if measure not in _MEASURES:
		frappe.throw(frappe._("Unknown notifications measure {0}").format(measure))
	read, fieldtype = _MEASURES[measure]
	return {"value": read(), "fieldtype": fieldtype}


def _days(days):
	start = _since(days)
	return [add_days(start, i) for i in range(days)]


def daily(view: str, days=TREND_DAYS) -> dict:
	"""One chart's {labels, datasets} over the last `days`, from one grouped query on `creation`."""
	log, start = frappe.qb.DocType(LOG), _since(days)
	day = Date(log.creation)
	if view == "read_rate":
		rows = (
			frappe.qb.from_(log).select(day.as_("day"), Count("*").as_("n"), Sum(log.read).as_("r"))
			.where(log.creation >= start).groupby(day)
		).run(as_dict=True)
		# Only days that sent anything: a quiet day has no read rate, and plotting it as 0% would read as a failure.
		by_day = sorted((getdate(r.day), round(100 * flt(r.r) / r.n, 1)) for r in rows if r.n)
		return {"labels": [d.strftime("%d %b") for d, _ in by_day], "datasets": [{"name": frappe._("Read %"), "values": [v for _, v in by_day]}]}
	series = {"type": log.type, "app": log.app, "push": Case().when(_unstamped(log), NOT_REACHED).else_(log.push_state)}.get(view)
	if series is None:
		frappe.throw(frappe._("Unknown notifications view {0}").format(view))
	rows = (
		frappe.qb.from_(log).select(day.as_("day"), series.as_("k"), Count("*").as_("n"))
		.where(log.creation >= start).groupby(day, series)
	).run(as_dict=True)
	counts = {}
	for r in rows:
		counts.setdefault(r.k or frappe._("Other"), {})[getdate(r.day)] = r.n
	dates = _days(days)
	return {
		"labels": [d.strftime("%d %b") for d in dates],
		"datasets": [{"name": k, "values": [v.get(d, 0) for d in dates]} for k, v in sorted(counts.items())],
	}


def _opted_in(field) -> dict:
	"""{type: users who ticked it on this channel and keep the channel's master on}."""
	pref, settings = frappe.qb.DocType(PREFERENCE), frappe.qb.DocType(api.USER_SETTINGS)
	master = settings.enable_email_notifications if field == EMAIL_FIELD else settings[api.PUSH_MASTER]
	rows = (
		frappe.qb.from_(pref).join(settings).on(pref.parent == settings.name)
		.select(pref.notification_type.as_("t"), Count("*").as_("n"))
		.where((pref.parenttype == api.USER_SETTINGS) & (pref.parentfield == field) & (master == 1))
		.groupby(pref.notification_type)
	).run(as_dict=True)
	return {r.t: r.n for r in rows}


def by_type(days=WINDOW_DAYS) -> list[dict]:
	"""One row per notification type with any activity or any opt-in: written, read, read %, push sent, who opted in."""
	log = frappe.qb.DocType(LOG)
	rows = (
		frappe.qb.from_(log)
		.select(log.type.as_("type"), Count("*").as_("written"), Sum(log.read).as_("read"),
			Sum(Case().when(log.push_state == push.SENT, 1).else_(0)).as_("push_sent"))
		.where(log.creation >= _since(days)).groupby(log.type)
	).run(as_dict=True)
	activity = {r.type: r for r in rows}
	email, pushed = _opted_in(EMAIL_FIELD), _opted_in(PUSH_FIELD)
	out = []
	for t in sorted(set(activity) | set(email) | set(pushed), key=lambda t: -(activity[t].written if t in activity else 0)):
		a = activity.get(t) or frappe._dict(written=0, read=0, push_sent=0)
		written, read = cint(a.written), cint(a.read)
		out.append({
			"type": t, "written": written, "read": read, "read_rate": round(100 * read / written, 1) if written else 0,
			"email_on": email.get(t, 0), "push_on": pushed.get(t, 0), "push_sent": cint(a.push_sent),
		})
	return out
