# TEMPORARY — migration reconciliation demo, remove before prod. See REMOVE-ME.md
"""The endpoints the page calls — one per visible step.

WHY SO MANY SMALL CALLS. The partner API meters its list endpoints as bulk, and the bulk bucket
refills once every few seconds, so four counts cost roughly four waits no matter how they are
issued. Sending them as one long request would hold a worker for the whole time and show the
operator nothing; sending them as four short ones costs the same wall-clock, frees the worker
between each, and lets the page tick a real step as each figure lands.

Nothing here trusts the browser for identity: the ProspectID is re-resolved server-side on every
call, so a caller cannot point a count at someone else's lead.

Credentials never leave the server.
"""

from contextlib import contextmanager

import frappe
from frappe.rate_limiter import rate_limit

from tatva_connect.migration_check import (
	audit,
	batch,
	compare,
	guard,
	jobs,
	limits,
	storage,
	totals,
)
from tatva_connect.migration_check import constants as C

CACHE_TTL = 60  # seconds — several people clicking during a demo must not hammer LeadSquared

# THE LOOPBACK BUDGET. The Frappe figures are read over HTTP from this box back to itself, so a
# worker answering `crm_count` holds its slot while a SECOND worker serves the partner API call.
# Under the bulk gate that wait can run several seconds. Left unbounded, enough simultaneous
# lookups would occupy every gunicorn slot waiting on slots that no longer exist — the server
# would deadlock on itself. This caps how many of those round trips may be in flight at once;
# past the cap the tool refuses immediately instead of queueing and eating a worker.
MAX_INFLIGHT = 3
INFLIGHT_TTL = 60  # a crashed worker must not leak its slot forever


def _prepare(prospect_id: str | None, grain: str | None):
	"""Shared entry work: gate, validate, resolve the grain and its credentials."""
	guard.assert_permitted()

	prospect_id = (prospect_id or "").strip()
	if not prospect_id:
		frappe.throw("Enter a LeadSquared ProspectID.", frappe.ValidationError)

	slug = (grain or "").strip()
	if slug not in C.GRAINS:
		frappe.throw("Select a grain.", frappe.ValidationError)

	return prospect_id, C.Grain(slug), guard.credentials(slug)


@frappe.whitelist(methods=["GET"])
def options():
	"""Everything the page needs to draw itself: grains, labels and the wording of the rules."""
	guard.assert_permitted()
	return {
		"grains": guard.available_grains(),
		"labels": C.RESOURCE_LABELS,
		"lsq_only": C.LSQ_ONLY_RESOURCES,
		"context_only": C.CONTEXT_ONLY_RESOURCES,
		"notes": C.EXPECTED_DIFFERENCES,
		"indicative": C.INDICATIVE_RESOURCES,
		"budget": limits.snapshot(),
		"roles": list(guard.roles()),
	}


@frappe.whitelist(methods=["GET"])
@rate_limit(limit=limits.LOOKUP_PER_HOUR, seconds=limits.HOUR, ip_based=True)
def locate(prospect_id: str | None = None, grain: str | None = None):
	"""Step 1 — find the lead in Frappe and confirm it belongs to the chosen grain.

	This is the entry point of a lookup, so the per-user and site-wide limits are spent here rather
	than on every step. It is a single indexed read on a unique column.
	"""
	prospect_id, resolved, _creds = _prepare(prospect_id, grain)
	limits.check_lookup()

	found = compare.locate(prospect_id, resolved.slug)
	audit.log_call(
		"lead_check",
		{"prospect_id": prospect_id, "grain": resolved.slug},
		output={"lead": found["lead_name"], "grain_mismatch": bool(found["grain_mismatch"])},
	)
	return {
		"prospect_id": prospect_id,
		"grain": resolved.slug,
		"grain_label": resolved.label,
		"lead_name": found["lead_name"],
		"program": found["program"],
		"grain_mismatch": found["grain_mismatch"],
		"resolved_via": "LSQ ProspectID stored on the lead" if found["lead_name"] else None,
		"fetches": resolved.fetches,
		"compares": resolved.compares,
		"steps": _steps(resolved),
		"event_catalogue": resolved.event_catalogue(),
	}


@frappe.whitelist(methods=["GET"])
def lsq(prospect_id: str | None = None, grain: str | None = None):
	"""Step 2 — the LeadSquared side, in one pass over that lead's records."""
	prospect_id, resolved, creds = _prepare(prospect_id, grain)

	cached = _cached(f"lsq:{resolved.slug}:{prospect_id}")
	if cached:
		return cached

	try:
		side = compare.lsq_side(creds, resolved, prospect_id)
	except Exception as exc:
		message = _message(exc)
		audit.log_call(
			"leadsquared_read", {"prospect_id": prospect_id, "grain": resolved.slug}, error=message
		)
		return {"found": False, "error": message, "counts": {}, "by_event_code": []}

	audit.log_call(
		"leadsquared_read",
		{"prospect_id": prospect_id, "grain": resolved.slug},
		output=side.get("counts"),
		calls=side.get("calls") or 0,
	)

	result = {
		"found": side.get("found", False),
		"error": None,
		"calls": side.get("calls") or 0,
		"read_at": frappe.utils.format_datetime(frappe.utils.now_datetime(), "d MMM yyyy, HH:mm"),
		"counts": side.get("counts") or {},
		"by_event_code": side.get("by_event_code") or [],
		"out_of_scope_activities": side.get("out_of_scope_activities") or 0,
	}
	return _store(f"lsq:{resolved.slug}:{prospect_id}", result)


@frappe.whitelist(methods=["GET"])
def crm_count(prospect_id: str | None = None, grain: str | None = None, resource: str | None = None):
	"""Steps 3..n — ONE Frappe figure, through the partner API. One step, one tick."""
	prospect_id, resolved, creds = _prepare(prospect_id, grain)

	if resource not in resolved.fetches:
		frappe.throw("Unknown resource for this grain.", frappe.ValidationError)

	cached = _cached(f"count:{resolved.slug}:{prospect_id}:{resource}")
	if cached:
		return cached

	lead_name = compare.locate(prospect_id, resolved.slug)["lead_name"]
	if not lead_name:
		return {"resource": resource, "count": None, "by_type": None, "error": "Lead not found in Frappe."}

	try:
		with _loopback_slot():
			got = compare.crm_count(creds, lead_name, resource, _base_url(), _host())
	except frappe.ValidationError:
		raise
	except Exception as exc:
		return {"resource": resource, "count": None, "by_type": None, "error": _message(exc)}

	result = {
		"resource": resource,
		"count": got["count"],
		"by_type": got.get("by_type"),
		"error": None,
	}
	return _store(f"count:{resolved.slug}:{prospect_id}:{resource}", result)


@frappe.whitelist(methods=["GET"])
def fields(prospect_id: str | None = None, grain: str | None = None):
	"""Final step — the field-by-field comparison."""
	prospect_id, resolved, creds = _prepare(prospect_id, grain)

	cached = _cached(f"fields:{resolved.slug}:{prospect_id}")
	if cached:
		return cached

	lead_name = compare.locate(prospect_id, resolved.slug)["lead_name"]
	if not lead_name:
		return {"fields": [], "unmapped_fields": [], "error": "Lead not found in Frappe."}

	try:
		with _loopback_slot():
			mapped, unmapped = compare.field_comparison(
				creds, resolved, prospect_id, lead_name, _base_url(), _host()
			)
	except frappe.ValidationError:
		raise
	except Exception as exc:
		return {"fields": [], "unmapped_fields": [], "error": _message(exc)}

	result = {"fields": mapped, "unmapped_fields": unmapped, "error": None}
	return _store(f"fields:{resolved.slug}:{prospect_id}", result)


# -- background runs: control totals and sample batches -----------------------


@frappe.whitelist(methods=["GET"])
@rate_limit(limit=limits.RUN_PER_HOUR, seconds=limits.HOUR, ip_based=True)
def start_totals(grain: str | None = None):
	"""Queue a control-totals run for one grain. Returns the run id to follow."""
	guard.assert_permitted()
	slug = (grain or "").strip()
	if slug not in C.GRAINS:
		frappe.throw("Select a grain.", frappe.ValidationError)
	guard.credentials(slug)  # refuses a grain that is not configured here

	limits.check_run_launch()
	jobs.check_daily_budget(slug)
	run_id = totals.run_id_for(slug)
	jobs.acquire_lock(run_id)
	jobs.spend_daily_budget(slug)

	jobs.enqueue(
		"tatva_connect.migration_check.totals.execute",
		run_id,
		grain=slug,
		user=frappe.session.user,
	)
	return {"run_id": run_id, "budget_left": jobs.budget_left(slug)}


@frappe.whitelist(methods=["GET"])
@rate_limit(limit=limits.RUN_PER_HOUR, seconds=limits.HOUR, ip_based=True)
def start_batch(grain: str | None = None, prospect_ids: str | None = None):
	"""Queue a sample run. Capped at 100 ids — that is already 400 LeadSquared calls."""
	guard.assert_permitted()
	slug = (grain or "").strip()
	if slug not in C.GRAINS:
		frappe.throw("Select a grain.", frappe.ValidationError)
	guard.credentials(slug)

	ids = jobs.clean_ids(prospect_ids or "")
	limits.check_run_launch()
	jobs.check_daily_budget(slug)
	run_id = batch.run_id_for(slug)
	jobs.acquire_lock(run_id)
	jobs.spend_daily_budget(slug)

	jobs.enqueue(
		"tatva_connect.migration_check.batch.execute",
		run_id,
		grain=slug,
		prospect_ids=ids,
		user=frappe.session.user,
	)
	return {"run_id": run_id, "count": len(ids), "budget_left": jobs.budget_left(slug)}


@frappe.whitelist(methods=["GET"])
def run(run_id: str | None = None):
	"""One run's current state. The pages poll this while a job is going.

	Deliberately cheap and deliberately NOT rate-limited hard: it is a cache read plus a file read,
	and throttling it would only make a running job look frozen. The live counters come from Redis,
	which a running job updates after every lead, so progress moves even between file writes.
	"""
	guard.assert_permitted()
	run_id = (run_id or "").strip()
	found = storage.read(run_id)
	if not found:
		frappe.throw("That run was not found.", frappe.ValidationError)

	live = storage.read_progress(run_id)
	if live and found.get("status") == "running":
		found["done"] = max(found.get("done") or 0, live.get("done") or 0)
		found["total"] = live.get("total") or found.get("total")
		found["summary"] = live.get("summary") or found.get("summary")
	return found


@frappe.whitelist(methods=["GET"])
def runs(kind: str | None = None):
	"""Recent runs, plus what is happening right now and what budget is left."""
	guard.assert_permitted()
	return {
		"runs": storage.listing(kind),
		"running": jobs.current_run(),
		"budget": {g["slug"]: jobs.budget_left(g["slug"]) for g in guard.available_grains()},
		"max_ids": jobs.MAX_BATCH_IDS,
		"max_runs_per_day": jobs.MAX_RUNS_PER_DAY,
	}


@frappe.whitelist(methods=["GET"])
def download(run_id: str | None = None):
	"""The run as CSV — the artifact an operator files or forwards."""
	guard.assert_permitted()
	found = storage.read((run_id or "").strip())
	if not found:
		frappe.throw("That run was not found.", frappe.ValidationError)

	frappe.local.response.filename = f"{found['run_id']}.csv"
	frappe.local.response.filecontent = storage.to_csv(found)
	frappe.local.response.type = "download"


# -- helpers -----------------------------------------------------------------


def _steps(grain: C.Grain) -> list[dict]:
	"""The progress checklist the page draws. Derived from the grain, never hardcoded."""
	steps = [
		{"key": "locate", "label": "Locating the lead in Frappe"},
		{"key": "lsq", "label": "Reading LeadSquared"},
	]
	steps += [
		{"key": f"count:{r}", "label": f"Counting {C.RESOURCE_LABELS.get(r, r).lower()} in Frappe"}
		for r in grain.fetches
	]
	steps.append({"key": "fields", "label": "Comparing field values"})
	return steps


def _cached(key: str):
	hit = frappe.cache().get_value(f"migration_check:{key}")
	if hit:
		hit["cached"] = True
	return hit


def _store(key: str, value: dict) -> dict:
	value["cached"] = False
	frappe.cache().set_value(f"migration_check:{key}", value, expires_in_sec=CACHE_TTL)
	return value


def _message(exc: Exception) -> str:
	text = str(exc).strip()
	if not text:
		audit.log_error("endpoint")
		return "Unexpected error — see the error log."
	return text[:300]


@contextmanager
def _loopback_slot():
	"""Hold one of the MAX_INFLIGHT loopback permits, or refuse.

	The counter is always released, including on error, and carries a TTL so a killed worker
	cannot strand a permit.
	"""
	cache = frappe.cache()
	key = "migration_check:inflight"
	taken = cache.incr(key)
	cache.expire(key, INFLIGHT_TTL)

	if taken > MAX_INFLIGHT:
		cache.decr(key)
		frappe.throw("The checker is busy with other lookups. Try again in a moment.", frappe.ValidationError)
	try:
		yield
	finally:
		cache.decr(key)


def _base_url() -> str:
	"""Where the partner API answers, from wherever the caller happens to run.

	A web request can use loopback: the same container serves it. A BACKGROUND JOB CANNOT — it runs
	in a worker container with no gunicorn of its own, so 127.0.0.1 refuses the connection. On a
	containerised site set `migration_check_base_url` to the backend service
	(e.g. "http://backend:8000"), which resolves from every container including the backend itself.
	"""
	configured = frappe.conf.get("migration_check_base_url")
	if configured:
		return str(configured).rstrip("/")
	port = frappe.conf.get("webserver_port") or 8000
	return f"http://127.0.0.1:{port}"


def _host() -> str:
	"""Frappe routes by Host header, so loopback must carry the site name."""
	return frappe.conf.get("migration_check_host") or frappe.local.site
