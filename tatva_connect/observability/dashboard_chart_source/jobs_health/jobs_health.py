"""Data method for the 'Jobs Health' Dashboard Chart Source.

Thin: picks a view, asks `observability.jobs_health` for it, and returns the Frappe chart shape
{labels, datasets}. The computation is not repeated here — the cards read the same functions.

DELIBERATELY NOT CACHED. `cache_source` exists for a rollup table that cannot change between reads;
a queue depth changes every second, and a cached one is a wrong number wearing a chart. Each view is
a handful of redis reads, which is what a live figure costs.
"""
import frappe

from tatva_connect.observability import jobs_health


def _series(label, mapping):
	return {"labels": list(mapping.keys()), "datasets": [{"name": label, "values": list(mapping.values())}]}


@frappe.whitelist()
def get(chart_name=None, chart=None, no_cache=None, filters=None, from_date=None, to_date=None,
	time_interval=None, timespan=None, heatmap_year=None, **kwargs):
	frappe.only_for("System Manager")  # the gate RQ Job, RQ Worker and Scheduled Job Type already carry
	parsed = frappe.parse_json(filters) if filters else None
	view = parsed.get("view") if isinstance(parsed, dict) else None  # the config UI can send a list form

	if view == "status":
		return _series(frappe._("Jobs"), jobs_health.jobs_in_flight())
	if view == "scheduled":
		return _series(frappe._("Scheduled jobs"), jobs_health.scheduled_jobs())

	depth, workers = jobs_health.queue_depth(), jobs_health.workers_by_lane()
	lanes = list(depth.keys())
	return {
		"labels": lanes,
		"datasets": [
			{"name": frappe._("Queued"), "values": [depth[lane] for lane in lanes]},
			{"name": frappe._("Workers"), "values": [workers.get(lane, 0) for lane in lanes]},
		],
	}
