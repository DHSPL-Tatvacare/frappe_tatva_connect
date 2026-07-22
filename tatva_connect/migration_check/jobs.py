# TEMPORARY — migration reconciliation demo, remove before prod. See REMOVE-ME.md
"""Limits on the two background jobs, and how they are queued.

These jobs spend a real external budget: a 100-lead batch is 400 LeadSquared calls. Left open,
a few impatient clicks would burn the account's daily quota and get the migration itself throttled.
Three guards, all Redis, all fail-closed:

  * ONE RUN AT A TIME per site — a second request is told so, never queued;
  * a DAILY CAP per grain — refused with the count, so the operator knows why;
  * a HARD CEILING on batch size, checked before anything is enqueued.

Everything runs on the `long` queue. Nothing here holds a web worker.
"""

import frappe

MAX_BATCH_IDS = 100  # operator's ceiling; 100 leads is 400 LeadSquared calls
MAX_RUNS_PER_DAY = 10  # per grain — roughly a 4,000-call/day ceiling
RUN_LOCK_TTL = 3600  # a crashed job must not hold the lock forever
DAY_TTL = 86400
QUEUE = "long"
JOB_TIMEOUT = 3600


class Busy(frappe.ValidationError):
	"""A run is already going, or the daily budget is spent."""


def _cache():
	return frappe.cache()


# -- one run at a time -------------------------------------------------------


def acquire_lock(run_id: str) -> None:
	"""Claim the single run slot, or raise. The SETNX is the lock."""
	cache = _cache()
	if not cache.set("migration_check:run_lock", run_id, nx=True, ex=RUN_LOCK_TTL):
		holder = cache.get("migration_check:run_lock")
		holder = holder.decode() if isinstance(holder, bytes) else holder
		frappe.throw(f"A check is already running ({holder}). Wait for it to finish, then try again.", Busy)


def release_lock(run_id: str) -> None:
	"""Release only if we still hold it — never stamp on a later run's lock."""
	cache = _cache()
	holder = cache.get("migration_check:run_lock")
	holder = holder.decode() if isinstance(holder, bytes) else holder
	if holder == run_id:
		cache.delete("migration_check:run_lock")


def current_run() -> str | None:
	holder = _cache().get("migration_check:run_lock")
	return holder.decode() if isinstance(holder, bytes) else holder


# -- daily budget ------------------------------------------------------------


def _day_key(grain: str) -> str:
	return f"migration_check:runs:{grain}:{frappe.utils.today()}"


def check_daily_budget(grain: str) -> int:
	"""Remaining runs for this grain today. Raises when spent."""
	used = int(_cache().get(_day_key(grain)) or 0)
	if used >= MAX_RUNS_PER_DAY:
		frappe.throw(
			f"The daily limit of {MAX_RUNS_PER_DAY} checks for this programme has been reached. "
			"It resets tomorrow.",
			Busy,
		)
	return MAX_RUNS_PER_DAY - used


def spend_daily_budget(grain: str) -> None:
	cache = _cache()
	key = _day_key(grain)
	cache.incr(key)
	cache.expire(key, DAY_TTL)


def budget_left(grain: str) -> int:
	return max(0, MAX_RUNS_PER_DAY - int(_cache().get(_day_key(grain)) or 0))


# -- input ceiling -----------------------------------------------------------


def clean_ids(raw: str | list) -> list[str]:
	"""Parse a pasted list into unique ids, refusing above the ceiling.

	Accepts newlines, commas or spaces — an operator pastes from a spreadsheet, not JSON.
	"""
	if isinstance(raw, str):
		parts = raw.replace(",", "\n").replace(" ", "\n").split("\n")
	else:
		parts = list(raw or [])

	seen, out = set(), []
	for part in parts:
		pid = str(part).strip()
		if not pid or pid in seen:
			continue
		seen.add(pid)
		out.append(pid)

	if not out:
		frappe.throw("Paste at least one ProspectID.", frappe.ValidationError)
	if len(out) > MAX_BATCH_IDS:
		frappe.throw(
			f"{len(out)} ProspectIDs pasted. The limit is {MAX_BATCH_IDS} per check — "
			f"that is already {MAX_BATCH_IDS * 4} LeadSquared calls.",
			frappe.ValidationError,
		)
	return out


# -- queueing ----------------------------------------------------------------


def enqueue(method: str, run_id: str, **kwargs) -> None:
	"""Hand off to the long queue. Never runs inside the web request."""
	frappe.enqueue(
		method,
		queue=QUEUE,
		timeout=JOB_TIMEOUT,
		job_id=f"migration_check:{run_id}",
		run_id=run_id,
		**kwargs,
	)
