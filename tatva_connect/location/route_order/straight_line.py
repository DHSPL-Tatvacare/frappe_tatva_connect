"""Straight line: nearest-next from where the rep stands, by great-circle distance. No call leaves the server."""
from tatva_connect.location.api import haversine
from tatva_connect.location.route_order.contract import Ordered

LABEL = "Straight line"


def order(origin, stops):
	left, here, ordered, total = list(stops), origin, [], 0.0
	while left:
		step = min(left, key=lambda s: metres(here, s))
		total += metres(here, step)
		ordered.append(step)
		left.remove(step)
		here = step
	return Ordered(ordered, LABEL, False, round(total))


def metres(a, b):
	return haversine(a["lat"], a["lng"], b["lat"], b["lng"])
