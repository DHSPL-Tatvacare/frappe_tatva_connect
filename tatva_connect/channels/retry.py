# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""How work we are OWED is retried until it lands or its budget is spent, and how a pass over it is booked — the policy, expressed once.

TWO LEDGERS KEEP THIS STATE AND THEY CANNOT SHARE A TABLE. A recording is one call's, so it lives on
`CRM Call Media` keyed on the call; WhatsApp media is fetched once but stored PER LEAD, so its outcome
belongs on each message row. Forcing one table would buy symmetry by making both harder to read.

What they must never disagree on is the POLICY — how long to wait, what counts as spent, and what a spent
row becomes. Written twice that is one rule in two places, and the day they drift nobody finds out. So the
ladder and the transition live here, and neither ledger owns a copy of either.

WHAT IS DELIBERATELY NOT HERE: where the state is written, what is fetched, and what "it landed" means.
Those genuinely differ, and they stay with the ledger that owns them.
"""
import frappe
from frappe.utils import add_to_date, now_datetime

# The states both ledgers share. A ledger may add its own — `call_media` has ABSENT, because a call can be known to have produced no audio, while a WhatsApp message that carried no media simply has no state at all.
AWAITING = "Awaiting"
STORED = "Stored"
ABANDONED = "Abandoned"

# Minutes to wait before each next attempt. The ladder's LENGTH is the budget: five tries over ~31 hours, spread so a provider's bad minute costs one attempt and its bad day costs three.
LADDER = (5, 15, 60, 360, 1440)
BUDGET = len(LADDER)


def book(sweep, job_id, queue="long"):
	"""Queue one pass over owed work — `long` by default, since a cron entry's 300s is too short for a batch; `deduplicate` refuses a second pass while one is queued or running, and returns None."""
	return frappe.enqueue(sweep, queue=queue, job_id=job_id, deduplicate=True, now=bool(frappe.flags.get("in_test")))


def first_attempt_at():
	"""When a row whose inline attempt just failed becomes due. The first rung, never a bare `now`."""
	return add_to_date(now_datetime(), minutes=LADDER[0])


def spend(attempts: int):
	"""One attempt spent: returns `(state, next_attempt_at)` — the ONE transition both ledgers make.

	`attempts` COUNTS the attempt just spent, so the first call passes 1. A spent budget answers
	ABANDONED with no next attempt, which is what keeps it out of a due-row query filtering on
	`next_attempt <= now` — the terminal state needs no second flag to be excluded.
	"""
	spent = attempts >= BUDGET
	return (
		ABANDONED if spent else AWAITING,
		None if spent else add_to_date(now_datetime(), minutes=LADDER[attempts - 1]),
	)
