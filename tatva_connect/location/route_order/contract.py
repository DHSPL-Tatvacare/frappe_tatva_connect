from dataclasses import dataclass


@dataclass(frozen=True)
class Ordered:
	"""What every route-order provider returns: the caller's stops in visiting order, who ordered them, whether by road, and the total length."""

	stops: list
	provider: str  # the LABEL of the provider that actually answered
	by_road: bool  # a road distance, or straight-line — the panel says which, so one is never read as the other
	distance_m: int
	polyline: str = ""  # the road path, Google's encoded-polyline string; blank means draw straight between the stops
