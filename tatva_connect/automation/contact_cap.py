# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""W12 — the platform ceiling on how often ONE NUMBER is contacted by the automation engine.

WHAT IT IS FOR. Every workflow is individually reasonable and the patient is on six of them. Nothing in
the engine has ever asked "how many times have we already contacted this person this month?", because
until W12 nothing recorded who was contacted at all. This is that question, asked once, in one place.

IT COUNTS THE STEP LOG, which is the record of what the engine actually did — not the WhatsApp Message
table and not the Call Log, because those are per-channel and the cap is about a PERSON. One patient
reached by WhatsApp and by voice is two rows with an IDENTICAL `contact` (`sends._canonical_contact`),
which is the whole reason that column stores the canonical form.

MOBILE ONLY. Email is not counted: a ceiling enforced on a phone number cannot have an address folded
into it and still mean anything.

HARD, WHICH MEANS SOMETHING HAS TO BE LOCKED. The count and the write that follows it must not be
interruptible, or two sends both read "4 of 5" and both go. The lock is the SETTINGS row — zero new
doctypes, and the send already runs on a paced worker lane so sends do not arrive in a true burst. The
lock is taken inside the segment's transaction and released when the segment commits, which is exactly
when this send's own step-log row becomes visible to the next caller. A per-number row would let sends to
DIFFERENT numbers proceed in parallel and is the drop-in swap if this ever profiles badly.

WHAT WAS REJECTED, and why:
  * `api._base._bucket_pair`, the Redis token bucket the cohort drain paces itself with. It is a
    per-minute PACING device with a deliberate fail-open, and Redis has no persistence here. A ceiling
    measured in weeks that silently lifts when Redis restarts is not a ceiling.
  * `frappe.rate_limiter` — per-request, keyed on the caller, sized in seconds. Wrong axis entirely.
  * Counting `WhatsApp Message` / `CRM Call Log` — two tables, two shapes, and neither knows about the
    other, so the cap would be per-channel by construction.

THE REFUSAL IS A `failed`, NOT AN EXCEPTION. It rides the path consent and a bad number already use, so
a capped send is something the author's own failed branch already handles and no new mechanism appears
in the graph.
"""
import frappe

SETTINGS_DT = "CRM Contact Cap Settings"
STEP_LOG_DT = "CRM Workflow Step Log"


def refusal(channel, contact):
	"""Why this send must not go out, or None. Called at the DECISION to send, before anything is queued.

	Returns the reason so the caller can hand it to its `failed` edge verbatim — the cap never raises and
	never stops the journey; the author's branch decides what happens next.
	"""
	from tatva_connect.automation import sends

	if channel not in sends.MOBILE_CHANNELS or not contact:
		return None  # email is not counted, and a number with no canonical identity has nothing to count
	if not _armed():
		return None  # asked BEFORE the lock: a switched-off cap must not serialise every send on this site
	settings = _locked_settings()
	already = _contacts_since(contact, settings.window_days())
	if already < settings.max_contacts:
		return None
	return (
		f"failed: this number has already been contacted {already} times in the last "
		f"{settings.window_count} {(settings.window_unit or 'days').lower()}, which is the limit"
	)


def _armed():
	"""Is the ceiling switched on? The CHEAP question, asked before any lock is taken.

	`get_single_value` is frappe's own cached reader for a Single, so the overwhelmingly common answer —
	no, the cap is off — costs nothing and blocks nobody. A site whose doctype has never been synced has
	no row at all, and that reads as off, which is the correct resting state rather than an error.

	Racing an operator arming the cap costs at most one send slipping through, which is the same tolerance
	every dormant switch in this app already has.
	"""
	return bool(frappe.db.get_single_value(SETTINGS_DT, "enabled"))


def _locked_settings():
	"""The operator's ceiling, with the row LOCKED for the rest of this transaction.

	`Singles` is an ordinary table, so the lock is `get_value(..., for_update=True)` — the house idiom,
	no raw SQL. Taking it here is what makes the cap hard: every other send that reaches this line blocks
	until this segment commits, and this segment's commit is what publishes its own step-log row. So the
	next caller counts a set that already includes this send, and two sends can never both see room.

	Read FRESH after the lock, never from the document cache: the point of waiting for the lock is to see
	what the winner left behind, and a cached copy would be the state from before the queue.
	"""
	# `order_by=None` because `Singles` has no `creation` column and `get_value`'s default ordering names
	# one — the read is a lock on a single known row, so there is nothing to order anyway.
	frappe.db.get_value(
		"Singles", {"doctype": SETTINGS_DT, "field": "enabled"}, "value", order_by=None, for_update=True,
	)
	return frappe.get_doc(SETTINGS_DT)


def _contacts_since(contact, days):
	"""How many times the engine has reached this number on a mobile channel inside the rolling window.

	ROLLING, never calendar: "ten a month" as a calendar month lets a patient be contacted ten times on
	the last day of one month and ten more on the first of the next — twenty in two days, cap technically
	honoured. The window is counted back from now.

	Seeks `ix_contact_creation` (contact, creation), which is covering for this count.
	"""
	from tatva_connect.automation import sends

	since = frappe.utils.add_to_date(frappe.utils.now_datetime(), days=-days)
	return frappe.db.count(STEP_LOG_DT, {  # authz-ok: tier-a — automation engine counting its own audit trail
		"contact": contact,
		"channel": ["in", list(sends.MOBILE_CHANNELS)],
		"creation": [">", since],
	})


def void(journey, node_id):
	"""Un-count a send the provider REJECTED at hand-off. ROLLS THE CALLER'S TRANSACTION BACK, then writes
	the un-count and COMMITS it — because the write has to outlive the raise that always follows it.

	THAT IS THE CONTRACT, not an implementation detail, and a caller with pending work of its own must not
	use this. Both callers today void and then raise immediately — `sends._deliver_whatsapp` throws the
	provider's reason, `sends._deliver_voice` re-raises Bolna's own error — and that exception rolls the
	job's transaction back. Left pending, the un-count is undone with it: the slot was never actually
	given back on either channel, which is the whole defect this line closes. The house pattern for a
	write that must survive a raise is `observability/capture.py`'s — roll back first so this is the only
	pending write, then commit exactly it.

	THE APIS REJECTED, named. A bare `frappe.db.commit()` without the rollback: it would publish whatever
	else the job left pending, which is a behaviour change wider than the un-count. `frappe.db.savepoint`:
	the outer rollback destroys savepoints, so it survives nothing. A second connection: a new mechanism
	for a write frappe can already make durable.

	NOTHING IS PENDING AT EITHER CALL SITE — checked per path, not assumed. `_deliver_whatsapp` reaches
	its refusal having only READ: the account, the adapter module, the template row and the token. WATI's
	`_classify` is pure by its own docstring and `_post` writes no Integration Request (that is
	`create_request_log`, which nothing here calls). Its one `log_error` is on the `unknown` branch, which
	returns before the refusal. `_deliver_voice` reaches its refusal through `connection_for` (two reads)
	and a `requests.post` that raises without touching the database at all.

	NO SAVEPOINT IS OPEN, and a full rollback would destroy one. `sends.py`'s only savepoint is taken on
	the ACCEPTED WhatsApp path, which the refusal can never reach; the two are disjoint TODAY, and anyone
	folding them together has to revisit this. The segment's own savepoint is long released — its deferred
	thunk only enqueues, and both callers run as the top frame of an RQ job with a transaction of its own.

	A TEST DRIVES A DIFFERENT TRANSACTION SHAPE. `enqueue_after_commit` never fires in a test that has not
	committed, so a test calls the delivery function directly and shares its transaction — where this
	rollback discards whatever it had not committed. A test commits its fixtures first and asserts the
	OUTCOME: the step row's `contact`, read back in a fresh transaction.

	It clears the CONTACT and not the row: what happened is still true and still readable, and the step
	keeps saying it was attempted. Only its claim on the ceiling is withdrawn.
	"""
	if not journey or not node_id:
		return False  # a verb that mints no engine token has no step to point at
	frappe.db.rollback()
	row = frappe.db.get_value(
		STEP_LOG_DT, {"journey": journey, "node_id": node_id, "contact": ["!=", ""]},
		"name", order_by="creation desc, name desc",
	)
	if not row:
		return False
	frappe.db.set_value(STEP_LOG_DT, row, "contact", "")  # authz-ok: tier-a — automation engine, queue context
	frappe.db.commit()
	return True
