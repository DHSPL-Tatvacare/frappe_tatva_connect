# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Job-lane health as NUMBERS ONLY — the one brain the Jobs cards and the Jobs charts both read.

WHAT LEAVES THIS MODULE, AND WHAT NEVER DOES. Every figure here is a count. No job name, no argument,
no traceback, no site config and no credential is returned, because the surface it feeds is a dashboard
and not a debugger: a failing job is opened in `RQ Job`, which carries its own traceback under its own
permission, and the platform's configuration is read in `CRM Control Tower`. A count cannot leak what a
payload holds, which is why the whole module is counts.

REDIS IS BENCH-WIDE, THE ANSWER IS NOT. A queue is shared by every site on the bench, so each job figure
is narrowed by frappe's own `filter_current_site_jobs` — without it a neighbouring site's backlog reads
as ours. The lane list is `get_queue_list()`, never a typed copy: `workflow` and `partner_bulk` are ours
and a new lane must appear here the day it is registered.

Every public entry is `frappe.only_for("System Manager")`. That is the same gate `RQ Job`, `RQ Worker`
and `Scheduled Job Type` already carry, so this module widens nothing.
"""

import frappe
from frappe.core.doctype.rq_job.rq_job import JOB_STATUSES, fetch_job_ids, filter_current_site_jobs
from frappe.utils.background_jobs import get_queue, get_queue_list, get_workers
from frappe.utils.scheduler import get_scheduler_status

SCHEDULED_JOB_DT = "Scheduled Job Type"


def _count(lane: str, status: str) -> int:
	"""This site's jobs in one lane at one status. The site filter is frappe's, not a second copy."""
	return len(filter_current_site_jobs(fetch_job_ids(get_queue(lane), status)))


def queue_depth() -> dict:
	"""Jobs waiting per lane. A lane climbing while its neighbours drain names the stuck consumer."""
	return {lane: _count(lane, "queued") for lane in get_queue_list()}


def jobs_by_status() -> dict:
	"""Every job this site has in redis, by status — the shape of the whole queue in one figure set."""
	return {status: sum(_count(lane, status) for lane in get_queue_list()) for status in JOB_STATUSES}


def workers_by_lane() -> dict:
	"""Worker PROCESSES serving each lane. A lane at zero is served by nobody, however deep it is.

	Asked per queue and never through `RQ Worker`: that doctype keeps only a worker whose `pid` is set
	(rq_worker.py:51), and on a bench whose workers run in their own containers that is none of them —
	four live workers draining jobs read as zero. `get_workers(queue)` is frappe's own per-queue form,
	answered off the `rq:workers:<queue>` registry, which is true wherever the process runs.
	"""
	return {lane: len(get_workers(get_queue(lane))) for lane in get_queue_list()}


def worker_count() -> int:
	"""Worker PROCESSES, de-duplicated. Summing `workers_by_lane` counts a worker once per lane it
	serves, which on this bench turned four processes into seven."""
	return len({w.name for lane in get_queue_list() for w in get_workers(get_queue(lane))})


def scheduled_jobs() -> dict:
	"""The roster split THREE ways, because `stopped` alone over-states the problem by the whole denylist.

	`scheduler_denylist.apply()` stops every third-party job this site can never use and re-stops it on
	every migrate, so a two-slice Running/Stopped chart reported 34 stopped when 34 of 34 were ours and
	deliberate. The denylist is read from the module that applies it, never re-typed here.
	"""
	from tatva_connect.scheduler_denylist import DENYLIST

	denied = {method for method, _reason in DENYLIST}
	stopped = frappe.get_all(SCHEDULED_JOB_DT, filters={"stopped": 1}, pluck="method")
	by_us = sum(1 for method in stopped if method in denied)
	return {
		"Running": frappe.db.count(SCHEDULED_JOB_DT, {"stopped": 0}),
		"Stopped by us": by_us,
		"Stopped elsewhere": len(stopped) - by_us,
	}


# Statuses whose registry is LIVE. `finished` is kept for ten minutes and `failed` for about a week, so
# a chart holding both compares two clocks and reads as a failure rate that was never measured.
IN_FLIGHT_STATUSES = ("queued", "started", "deferred", "scheduled")


def jobs_in_flight() -> dict:
	"""Work this site has accepted and not finished — every slice on the same clock, which is now."""
	return {status: sum(_count(lane, status) for lane in get_queue_list()) for status in IN_FLIGHT_STATUSES}


def _card(value: int) -> dict:
	"""The Custom number-card contract: `{value, fieldtype}`, and nothing a card cannot render."""
	return {"value": int(value), "fieldtype": "Int"}


@frappe.whitelist()
def card_scheduler_halted(filters=None) -> dict:
	"""1 when the scheduler is not running. Every timed job on this site is frozen while it reads 1."""
	frappe.only_for("System Manager")
	return _card(get_scheduler_status().get("status") != "active")


@frappe.whitelist()
def card_lanes_without_worker(filters=None) -> dict:
	"""Lanes holding work that no worker process serves — the failure that is silent everywhere else."""
	frappe.only_for("System Manager")
	workers, depth = workers_by_lane(), queue_depth()
	return _card(sum(1 for lane, n in depth.items() if n and not workers.get(lane)))


@frappe.whitelist()
def card_jobs_failed(filters=None) -> dict:
	"""Jobs that raised and are still in the failed registry — about a week's worth, not a live figure.

	Named `(Retained)` on the card for that reason: the registry is an accumulation with its own TTL,
	and calling it `Now` invited the reader to divide it by a ten-minute `finished` count.
	"""
	frappe.only_for("System Manager")
	return _card(sum(_count(lane, "failed") for lane in get_queue_list()))


@frappe.whitelist()
def card_jobs_queued(filters=None) -> dict:
	"""Work accepted and not yet started. Healthy at any depth that is falling, not at one that is not."""
	frappe.only_for("System Manager")
	return _card(sum(queue_depth().values()))
