"""Google Routes: road order from the Routes API `computeRoutes` with `optimizeWaypointOrder`, on the server key."""
from frappe.integrations.utils import make_post_request
from frappe.utils import cint

from tatva_connect.channels import transfer
from tatva_connect.location.route_order.contract import Ordered
from tatva_connect.location.route_order.straight_line import metres

LABEL = "Google Routes"
URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
# Google's own ceiling on `intermediates` in one request (Routes API, "Specify intermediate waypoints").
MAX_INTERMEDIATES = 25
# The road path rides in the same request, so drawing it costs no second call.
_FIELDS = "routes.optimizedIntermediateWaypointIndex,routes.distanceMeters,routes.polyline.encodedPolyline"


def fits(stops):
	"""Whether one request can carry the day: the farthest stop is the destination, the rest are intermediates."""
	return len(stops) - 1 <= MAX_INTERMEDIATES


def order(origin, stops, key):
	# Google reorders intermediates only, so the day ends at the stop farthest from where it starts.
	last = max(stops, key=lambda s: metres(origin, s))
	middle = [s for s in stops if s is not last]
	reply = make_post_request(
		URL,
		headers={"X-Goog-Api-Key": key, "X-Goog-FieldMask": _FIELDS, "Content-Type": "application/json"},
		json={
			"origin": _waypoint(origin),
			"destination": _waypoint(last),
			"intermediates": [_waypoint(s) for s in middle],
			"travelMode": "DRIVE",
			"optimizeWaypointOrder": True,
		},
		timeout=transfer.READ_TIMEOUT,
	)
	route = (reply.get("routes") or [{}])[0]
	sequence = route.get("optimizedIntermediateWaypointIndex")
	if sequence is None or sorted(sequence) != list(range(len(middle))):
		raise ValueError(f"Routes API answered without a usable order: {reply}")
	return Ordered(
		[middle[i] for i in sequence] + [last], LABEL, True, cint(route.get("distanceMeters")),
		(route.get("polyline") or {}).get("encodedPolyline") or "",
	)


def _waypoint(point):
	return {"location": {"latLng": {"latitude": point["lat"], "longitude": point["lng"]}}}
