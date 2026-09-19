"""The ONE read accessor — every gated automation asks `is_enabled("key")`."""
import frappe

from tatva_connect.automation.registry import is_guard, parent_of

# Entry points in the BULK lane; `frappe.local.job` is frappe's own (background_jobs.py:260) and absent in a web request, so an unlisted job runs live — never silently quiet.
_BULK_JOBS = frozenset({
	"tatva_connect.api.partner_bulk_worker.process_job",
})

# A bulk job's Bulk Lane (handbook ADR 01): Quiet loads data only, Live runs as live intake does; blank reads Quiet.
QUIET, LIVE = "Quiet", "Live"


def _in_bulk_lane() -> bool:
	job = getattr(frappe.local, "job", None)
	return bool(job) and job.get("method") in _BULK_JOBS


def _job_is_live() -> bool:
	"""The running bulk job's own lane, read off its row — the one place a job's Bulk Lane is stored."""
	name = (frappe.local.job.get("kwargs") or {}).get("bulk_job_id")
	return bool(name) and frappe.get_cached_value("CRM Bulk Job", name, "bulk_lane") == LIVE


def is_enabled(key: str) -> bool:
	"""On iff the row AND every ancestor it declares is on — the hierarchy is enforced here, nowhere else.

	TWO LANES, ONE SPINE. Inside a bulk job the JOB decides, not the toggle: a Quiet job keeps every toggle off
	except the data checks the registry marks `guard`, and a Live job answers exactly as a live request does.
	The lane is tested once, before the ancestor walk, so it can close a toggle but never open a disabled one.

	CACHED, and a flip still takes effect at once: frappe clears the document cache on BOTH write paths,
	`doc.save()` and `db.set_value` (database.py:993), and this codebase uses the latter 81 times. There
	is no stale window to reason about, which is what the previous uncached read was protecting.

	Uncached it cost one SELECT per check, and the wildcard doc_event makes EVERY save ask several times:
	measured 13 per CRM Task insert, 11 per File, 3 on a stock ToDo nobody here wrote. Four switches
	account for all of it; the other 57 are never read on a save. One line, 116 call sites, 66% off a save.

	An unknown key returns None -> False: fail-closed dormant. The walk carries no cycle guard because
	`assert_valid_graph` proved the chain terminates at import.
	"""
	if _in_bulk_lane() and not is_guard(key) and not _job_is_live():
		return False

	while key:
		if not frappe.get_cached_value("CRM Tatva Automation", key, "enabled"):
			return False
		key = parent_of(key)
	return True
