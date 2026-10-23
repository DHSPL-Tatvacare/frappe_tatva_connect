"""The ONE picker: CRM Maps Settings → Route Order names the provider — that setting is the only switch; Google also needs the server key."""
import hashlib
import json

import frappe

from tatva_connect.location import api as location_api
from tatva_connect.location.route_order import google_routes, straight_line
from tatva_connect.location.route_order.contract import Ordered

# Setting value -> provider; blank reads as straight line (a code fallback, never a seeded default).
PROVIDERS = {m.LABEL: m for m in (straight_line, google_routes)}
# The key carries the plan's date, so a day's entry is never read again once the day is over.
_TTL = 24 * 60 * 60


def order(origin, stops, scope, peek=False):
	"""`stops` in visiting order; the same order for one `scope` (rep + date) and stop set until a stop changes. Any Google trouble is logged and straight line answers.

	`peek` reads a remembered plan but never makes one: someone looking at another rep's day sees that rep's order if
	they have one, else a free straight-line preview — so a look never pays Google or fixes the rep's order from the wrong place."""
	wanted = google_routes if _wants_google(stops) else straight_line
	key = _key(scope, wanted, stops)
	if remembered := frappe.cache.get_value(key):
		return _replay(remembered, stops)
	if peek:
		return straight_line.order(origin, stops)
	ordered = _ask(wanted, origin, stops)
	# A fallback is never remembered, so a passing Google outage does not pin straight line for the day.
	if ordered.provider == wanted.LABEL:
		frappe.cache.set_value(key, _memo(ordered), expires_in_sec=_TTL)
	return ordered


def _ask(provider, origin, stops):
	if provider is not google_routes:
		return straight_line.order(origin, stops)
	try:
		return google_routes.order(origin, stops, location_api._api_key())
	except Exception:
		frappe.log_error(title="route order: Google Routes failed", message=frappe.get_traceback())
		return straight_line.order(origin, stops)


def _key(scope, provider, stops):
	"""Named by everything that decides the order — who, which day, which provider, each stop and where it is — so a change is a new key and nothing ever needs clearing."""
	where = sorted((s["id"], round(s["lat"], 6), round(s["lng"], 6)) for s in stops)
	digest = hashlib.sha1(json.dumps(where).encode()).hexdigest()
	return f"route_order:{scope}:{provider.LABEL}:{digest}"


def _memo(ordered):
	return {"ids": [s["id"] for s in ordered.stops], "provider": ordered.provider,
			"by_road": ordered.by_road, "distance_m": ordered.distance_m, "polyline": ordered.polyline}


def _replay(memo, stops):
	by_id = {s["id"]: s for s in stops}
	return Ordered([by_id[i] for i in memo["ids"]], memo["provider"], memo["by_road"], memo["distance_m"], memo["polyline"])


def _wants_google(stops):
	# Two stops or fewer have one sensible order, so Google is never paid to confirm it.
	return (
		len(stops) > 2
		and google_routes.fits(stops)
		and location_api._settings().get("route_order_provider") == google_routes.LABEL
		and bool(location_api._api_key())
	)
