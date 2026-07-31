# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""The dashboard, for whoever is asking. One endpoint, one round trip, one shape.

The caller sends a date range and the filters their layout offered them; it cannot name a chart, a list or
a column. A role with no layout is an expected state, so `configured: false` is a 200 and not an error.
"""

import hashlib

import frappe
from frappe import _
from frappe.utils import get_first_day, get_last_day, nowdate

from tatva_connect.dashboard import declaration, executor, resolver

# Ten aggregates over millions of rows cannot be made instant by indexing — the rows still have to be
# counted. So the first viewer of a given question pays for it and everyone else reads Redis, which is what
# frappe's own BI tool does by default. Short, because a dashboard that lies for long is worse than a slow one.
_CACHE_TTL = 300


@frappe.whitelist()
def get_dashboard(from_date=None, to_date=None, filters=None):
	layout = resolver.layout_for()
	if not layout:
		return {"configured": False, "charts": [], "filters": []}
	window = _window(from_date, to_date)
	exposed = frappe.parse_json(layout["exposed_filters"])
	chosen = _chosen(filters, exposed)
	key = _cache_key(layout, window, chosen)
	payload = frappe.cache().get_value(key)
	if payload is None:
		payload = _build(layout, window, exposed, chosen)
		frappe.cache().set_value(key, payload, expires_in_sec=_CACHE_TTL)
	return payload


def _cache_key(layout, window, chosen):
	"""The user, because the row gate differs per person, and the question that was asked. An operator's
	edit does not need to appear here: saving a card or a layout retires the whole namespace."""
	question = frappe.as_json([layout["name"], window, chosen], indent=None)
	# A digest, not frappe.generate_hash: that one ignores its argument and returns a random value, so every
	# request minted a new key — the cache never hit and Redis grew a key per request.
	digest = hashlib.blake2b(question.encode(), digest_size=8).hexdigest()
	return f"{declaration.CACHE_PREFIX}{frappe.session.user}:{digest}"


def _build(layout, window, exposed, chosen):
	placements = _placements(layout)
	declared = declaration.charts(placement["chart"] for placement in placements)
	return {
		"configured": True,
		"title": layout["title"] or "",
		"filters": exposed,
		"charts": [
			_card(placement, declared[placement["chart"]], window, chosen)
			for placement in placements
			if placement["chart"] in declared and executor.is_gated(declared[placement["chart"]])
		],
	}



def _chosen(filters, exposed):
	"""Narrowed to the controls this layout offers, so `exposed_filters` decides what is honoured and not
	merely what is drawn."""
	chosen = frappe.parse_json(filters) if filters else {}
	return {key: value for key, value in chosen.items() if key in exposed}


def _placements(layout):
	return frappe.parse_json(layout["layout"])


def _window(from_date, to_date):
	"""An unset range is the current month. Both or neither, so a half-given range cannot mix two months."""
	if not (from_date and to_date):
		from_date, to_date = get_first_day(nowdate()), get_last_day(nowdate())
	return {"from_date": str(from_date), "to_date": str(to_date)}


def _card(placement, chart, window, chosen):
	"""A declaration that throws costs its own card and no other."""
	card = {key: placement[key] for key in declaration.PLACEMENT}
	try:
		card.update(executor.run(chart, window, chosen))
	except Exception:
		frappe.log_error(title=f"Dashboard chart failed: {chart['chart_name']}", message=frappe.get_traceback())
		card.update(executor.envelope(chart, error=True))
	return card
