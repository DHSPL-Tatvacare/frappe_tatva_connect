"""Shared inbound-webhook spine — one front door for every provider.

Every inbound webhook takes the same moves, in this order:

  authenticate (webhooks.ingress; token, optional HMAC, optional IP)
  -> kill-switch (default OFF) -> persist the raw payload ONCE -> fast 2xx ACK and a kick
  -> the drain screens and works every stored row, oldest first.

The order is load-bearing. Authentication is first so that an unauthenticated flood can never write a
row, and the kill-switch is second so that a dormant integration still keeps an audit trail rather
than destroying what arrives. Screening happens in the drain, in arrival order, so a status is screened
only after the message it names — which arrived first — has been filed, and no declined row stays Queued.

Frappe's own log doctype does the storage, and Frappe's own helpers do the writing — a webhook is an
Integration Request, and `frappe.integrations.utils.create_request_log` creates one. The status
vocabulary is that doctype's, not an invented one:

  Queued     stored and waiting; the drain flips it.
  Completed  handled.
  Cancelled  deliberately not processed. The outcome says which gate declined it.
  Failed     the worker raised. The traceback is in `error`, so the DLQ is durable and queryable.

Retention is Frappe's too: `Integration Request` is registered in frappe's own
`default_log_clearing_doctypes` at 90 days, so nothing is registered here.

Concurrency is handled HERE, once, for every provider — no adapter writes a line for it:

  * The stored rows ARE the queue. A burst waits in the table, which has no cap, and never in RQ, which
    refused every enqueue past `max_queued_jobs` and left rows Queued with no job. ONE drain works them,
    booked by an atomic lock, so a thousand deliveries arriving together cannot race over booking it.
  * A provider re-sends. One live Acefone CDR arrived ELEVEN times, byte for byte. Each copy is its own
    row, worked in arrival order, so the first is handled and the rest find it already processed.
  * What still collides is re-run, not failed. The drain and a Desk replay can reach the writer
    together; the loser rolls back and runs again in place, where the adapter's `already_processed`
    sees the winner's committed row and completes.

None of this is a telephony concern. Every adapter — Acefone, Ozonetel, WATI, whatever comes next —
inherits all of it by coming through this door.

The door is keyed by CHANNEL, never by vendor. `receive("whatsapp", …)` authenticates a token, the
token resolves an account, and the account's own provider field names the adapter. No endpoint, no
URL and no log row carries a vendor's name, so swapping one is configuration rather than a deploy.

Adapter contract (duck-typed module, no ABC):
  * screen(payload, event, account)            -> (wanted, reason). Asked by the drain before a stored
    delivery is worked, and again on replay. The reason is written onto the declined row.
  * already_processed(payload, event, account) -> idempotency vs the target doctype
  * handle(payload, event, account)            -> parse + DB moves + fail-closed attribution
  * account_for_payload(payload, event)        -> re-derive the account on replay and reconcile
"""
import time

import frappe
from frappe import _
from frappe.integrations.utils import create_request_log

from tatva_connect.channels import resolve
from tatva_connect.utils import book_drain, release_drain
from tatva_connect.webhooks import ingress, registry
from tatva_connect.workflow_engine import thresholds

LOG_DOCTYPE = "Integration Request"
LANE = "short"

# The key that says a drain is booked. Its life is declared in `thresholds`, never restated here.
DRAIN_LOCK = "webhook-drain"

# Attempts a delivery gets when its write loses a race to another writer of the same call; Frappe's own budget for a deadlock.
MAX_RETRIES = 5

_GENERIC_DECLINE = "not accepted by this provider's pre-filter"

# A write that lost a race with another worker handling the SAME delivery. Not a failure: the four are the ways one collision surfaces — the winner still in flight (a deadlock, or MariaDB's 1020 snapshot conflict), the winner already committed and this one hit the primary key, or a unique column. Measured, not assumed: eleven concurrent workers writing one real Acefone CDR produced one row and nine of these.
CONFLICT = (
	frappe.QueryDeadlockError,
	frappe.QueryTimeoutError,
	frappe.DuplicateEntryError,
	frappe.UniqueValidationError,
)


def receive(channel, *, enabled, event=None):
	"""Shared front door. Authenticates, stores once, then ACKs fast; the drain decides.

	Authentication comes FIRST, before the kill-switch. That ordering is deliberate: a dormant
	integration must still keep an audit trail of what arrived — a switch that is off means "do not
	act on this", not "destroy it" — but only an authenticated caller may write a row, or an
	unauthenticated flood could fill the log table.

	`enabled` is the channel's kill-switch callback. The adapter is NOT passed in: the token resolves
	the account and the account names its provider, so the front door never has to be told — and no
	endpoint carries a vendor name it would have to be edited to change. Authentication is not a
	callback either: it is the same three factors for every channel and lives in `webhooks.ingress`.
	"""
	account = ingress.verify(channel)       # token digest -> HMAC -> IP; raises, fail-closed
	payload = _request_payload()            # form_dict minus cmd/token

	if not enabled():                       # kill-switch, fresh read, default OFF
		# Recorded, not discarded. Providers retry very few times — Acefone twice — so a call dropped while the switch was off would be gone for good. Logged Cancelled, it is replayable the moment the integration is turned on.
		_persist(channel, event, payload, account, "Cancelled", _declined("the integration is switched off"))
		return "ok"

	if not _persist(channel, event, payload, account, "Queued"):
		# The row IS the queue, so a delivery that could not be stored is refused with a 503 the provider retries, never acknowledged.
		raise frappe.ServiceUnavailableError("webhook spine: the delivery could not be stored")
	kick()
	return "ok"


# ---------------------------------------------------------------------------
# The queue is the stored rows, and ONE drain works them — `_delivery_key` went with the per-delivery job it keyed, archived in .archive/.
# ---------------------------------------------------------------------------
def kick():
	"""Book the one drain: the caller that takes the lock queues it, every other is told it is already booked."""
	if not _book():
		return None
	try:
		return frappe.enqueue(drain, queue=LANE)
	except Exception:
		# A lock is only worth holding for a drain that exists; hand it back so the next delivery books one.
		_release()
		raise


def _book():
	"""Take the one-drain lock, through the ONE booking the workflow wake drain shares."""
	return book_drain(DRAIN_LOCK, thresholds.WEBHOOK_DRAIN_LOCK_SECONDS)


def _release():
	"""Hand the booking back, so the next delivery can book a drain."""
	release_drain(DRAIN_LOCK)


def drain():
	"""Work stored deliveries oldest first until none are Queued or the slice is spent, then release the booking and hand what is left to the next drain."""
	if frappe.session.user == "Guest":
		frappe.set_user("Administrator")
	started, after = time.monotonic(), None
	try:
		while time.monotonic() - started < thresholds.WEBHOOK_DRAIN_SECONDS:
			rows = _queued(thresholds.WEBHOOK_DRAIN_BATCH, after)
			if not rows:
				break
			for row in rows:
				_work(row.name)
			after = rows[-1]
	finally:
		_release()
	# A spent slice leaves rows, and a delivery stored while this pass ended found the door locked: either way the next drain is booked here, never left to the next webhook.
	if _queued(limit=1):
		kick()


def _queued(limit, after=None):
	"""Stored deliveries still waiting, oldest first and past the cursor, so one pass visits a row once and never spins on it."""
	log = frappe.qb.DocType(LOG_DOCTYPE)
	query = (
		frappe.qb.from_(log)
		.select(log.name, log.creation)
		.where(log.integration_request_service.isin(list(registry.CHANNELS)))
		.where(log.status == "Queued")
		.orderby(log.creation)
		.orderby(log.name)
		.limit(limit)
	)
	if after:
		query = query.where((log.creation > after.creation) | ((log.creation == after.creation) & (log.name > after.name)))
	return query.run(as_dict=True)


def _work(name):
	"""Claim one stored delivery under a row lock, then re-screen and process it; a row another drain holds or finished is skipped."""
	# Each claim reads its own snapshot: a skipped row commits nothing, and a stale one makes the next locking read fail.
	frappe.db.commit()
	try:
		row = frappe.db.get_value(LOG_DOCTYPE, {"name": name, "status": "Queued"}, _STORED_FIELDS, as_dict=True, for_update=True, skip_locked=True)
	except (frappe.db.OperationalError, frappe.db.InternalError) as e:
		# A snapshot conflict IS a deadlock to frappe (`is_deadlocked` matches ER.CHECKREAD), and its answer to one is to run again: the row stays Queued for the next pass.
		if not (frappe.db.is_deadlocked(e) or frappe.db.is_timedout(e)):
			raise
		frappe.db.rollback()
		return
	if not row:
		return
	try:
		delivery, _reason = _rescreen(row)
		if delivery:
			process(delivery.channel, delivery.payload, delivery.account, vendor_event=delivery.vendor_event, log=name)
	except Exception:
		# The failure lands on the row with its traceback — written by `process` when it raised, or here for a row no provider recognises — and the drain moves on.
		frappe.db.rollback()
		_mark(name, "Failed", error=frappe.get_traceback())


def process(channel, payload, account, vendor_event=None, log=None):
	"""Worker: dedupe, then hand to the adapter. Runs privileged — the front door is a guest
	endpoint, but persistence and downstream automation must run as a system user.

	A raised handler lands its traceback on the log row, so the DLQ is durable in the database and
	replayable from the Desk. Nothing is lost behind a 200.
	"""
	if frappe.session.user == "Guest":
		frappe.set_user("Administrator")
	adapter = _adapter_for(channel, account)
	for attempt in range(1, MAX_RETRIES + 1):
		try:
			if adapter.already_processed(payload, vendor_event, account):
				_mark(log, "Completed", output={"outcome": "already processed"})
				return
			adapter.handle(payload, vendor_event, account)
			break
		except CONFLICT:
			# Another writer holds this same call: roll back and run again in place, where `already_processed` sees its committed row. Retried here, never through Frappe's `RetryBackgroundJobError`, whose re-run fails the job after it succeeds.
			frappe.db.rollback()
			if attempt == MAX_RETRIES:
				_mark(log, "Failed", error=frappe.get_traceback())
				raise
			time.sleep(attempt)
		except Exception:
			# Rolled back BEFORE the status is written. A half-finished handler must not have its partial writes flushed by the very commit that records the failure — the row would then be replayed over state it had already half-created.
			frappe.db.rollback()
			_mark(log, "Failed", error=frappe.get_traceback())
			raise
	_mark(log, "Completed")


# ---------------------------------------------------------------------------
# Outcome, decided by the drain — in arrival order, so a status is read after the message it names.
# ---------------------------------------------------------------------------
def _screen(adapter, payload, event, account):
	"""(wanted, reason), from the adapter's one screening call.

	A screen that raises is treated as wanted, so a bug in a filter can never silently discard a
	delivery: the worker will surface it properly, with a traceback, on a row an operator can replay.
	The reason is coerced to a string — a misbehaving adapter must not be able to break the logging
	that would have recorded its misbehaviour.
	"""
	try:
		wanted, reason = adapter.screen(payload, event, account)
	except Exception:
		frappe.log_error(title="webhook spine: screen raised", message=frappe.get_traceback())
		return True, None
	if wanted:
		return True, None
	return False, str(reason) if reason else _GENERIC_DECLINE


def _declined(reason):
	"""The outcome of a delivery the spine did not act on — one shape for every gate that declines."""
	return {"outcome": "not captured", "reason": reason}


# ---------------------------------------------------------------------------
# The raw log. One row per delivery, written once, never raising.
# ---------------------------------------------------------------------------
def _persist(channel, event, payload, account, status, outcome=None):
	"""Persist the delivery as an Integration Request and return its name, or None.

	Never raises: durable logging must not break a webhook. The privileged insert happens inside
	Frappe's own `create_request_log`, which is why no `ignore_permissions` marker appears here.
	"""
	try:
		row = create_request_log(
			payload,
			service_name=channel,
			request_description=f"{channel} {event or ''}".strip(),
			status=status,
			# Empty strings, not None. The helper runs both through `frappe.as_json`, which turns a None into the literal string "null" — which would then read as a populated Error field on every row. A str is passed through untouched.
			output=frappe.as_json(outcome) if outcome else "",
			error="",
			# The receiving account by name, never its token, so a stored delivery is worked against the account it arrived on; passed explicitly, so the helper never reads a `reference_*` key a provider posted.
			reference_doctype=registry.by_channel(channel)["account_doctype"],
			reference_docname=account,
		)
		return row.name
	except Exception:
		frappe.db.rollback()
		# The raw log is the durability guarantee behind the fast ACK — never lose a failure silently.
		frappe.log_error(title="webhook spine: raw-log persist failed", message=frappe.get_traceback())
		return None


def _mark(log, status, output=None, error=None):
	"""Flip the log row's status, and record why. Never raises — best-effort bookkeeping.

	`frappe.db.set_value` writes exactly the columns given, and `update_modified=False` leaves the
	timestamp alone so the row keeps saying when the delivery actually arrived rather than when its
	status was last touched.
	"""
	if not log:
		# The raw log never got written, so there is nowhere to record this. A traceback still has to survive somewhere the operator can find it.
		if error:
			frappe.log_error(title="webhook spine: handler failed with no log row", message=error)
		return

	values = {"status": status}
	if output is not None:
		values["output"] = frappe.as_json(output)
	if error is not None:
		values["error"] = error
	try:
		frappe.db.set_value(LOG_DOCTYPE, log, values, update_modified=False)
		frappe.db.commit()
	except Exception:
		frappe.log_error(title="webhook spine: log status flip failed", message=frappe.get_traceback())


# ---------------------------------------------------------------------------
# Adapter resolution (lazy import -> no import cycle; import-safe before adapters exist)
# ---------------------------------------------------------------------------
def _adapter_for(channel, account):
	"""The adapter module for this delivery — the channel's registry entry, then the ACCOUNT's own
	provider field. Lazy-imported, so the spine stays import-safe and free of any provider import.

	The account is what names the vendor, which is why nothing above ever had to, and why this always
	requires one. A stored row names the account it arrived on; only an older row, stored before the door
	stamped it, is resolved from the payload (`resolve.adapter_for_payload`).
	"""
	cfg = registry.by_channel(channel)
	if not cfg:
		frappe.throw(f"No webhook channel registered as {channel!r}")
	return resolve.adapter_for(account, cfg["account_doctype"])


def _request_payload():
	"""The provider's payload as a plain dict — form_dict minus Frappe's own keys."""
	return {k: v for k, v in frappe.form_dict.items() if k not in ("cmd", "token")}


# ---------------------------------------------------------------------------
# Replay — re-run the worker over stored raw payloads. System Manager only.
# ---------------------------------------------------------------------------
REPLAYABLE = ("Failed", "Cancelled")

# What `_rescreen` reads off a stored row — the claim in `_work` and the Desk replay fetch the same columns.
_STORED_FIELDS = ["name", "integration_request_service", "request_description", "data", "reference_docname"]


def _rescreen(row):
	"""A stored delivery re-screened against today's configuration: (delivery, None), or (None, reason) with the row left Cancelled."""
	channel = row.integration_request_service
	payload = frappe.parse_json(row.data) or {}
	event = _event_from_description(channel, row.request_description)
	if row.reference_docname:
		account, adapter = row.reference_docname, _adapter_for(channel, row.reference_docname)
	else:
		adapter, account = resolve.adapter_for_payload(channel, payload, event)
	if not adapter:
		frappe.throw(
			_("No {0} provider recognises this delivery, so it cannot be replayed against one.").format(channel),
			title=_("Unidentified delivery"),
		)

	wanted, reason = _screen(adapter, payload, event, account)
	if not wanted:
		_mark(row.name, "Cancelled", output=_declined(reason))
		return None, reason
	return frappe._dict(channel=channel, payload=payload, account=account, vendor_event=event), None


@frappe.whitelist()
def replay(integration_request):
	"""Re-run one stored delivery now; a Cancelled row lands once the configuration its reason names is fixed."""
	frappe.only_for("System Manager")
	row = frappe.db.get_value(LOG_DOCTYPE, integration_request, _STORED_FIELDS, as_dict=True)
	delivery, reason = _rescreen(row)
	if not delivery:
		return reason
	process(delivery.channel, delivery.payload, delivery.account, vendor_event=delivery.vendor_event, log=integration_request)
	return "ok"


@frappe.whitelist()
def replay_channel(channel, status="Failed", since=None):
	"""Put a channel's replayable rows back in the queue: Failed (the worker raised) or Cancelled (declined).

	Cancelled is the one an operator reaches for after fixing configuration — map a DID, then replay
	everything that was dropped for want of it. A row is Failed only once its worker actually raised,
	so this never re-runs still-in-flight work. The drain re-screens and works each row like any other.
	"""
	frappe.only_for("System Manager")
	if status not in REPLAYABLE:
		frappe.throw(_("Only {0} deliveries can be replayed.").format(" or ".join(REPLAYABLE)))
	if not registry.by_channel(channel):
		frappe.throw(_("No webhook channel registered as {0}.").format(channel))

	filters = {"integration_request_service": channel, "status": status}
	if since:
		filters["creation"] = [">=", since]
	queued = frappe.db.count(LOG_DOCTYPE, filters)
	frappe.db.set_value(LOG_DOCTYPE, filters, {"status": "Queued", "output": ""}, update_modified=False)
	frappe.db.commit()
	kick()
	return queued


def _event_from_description(channel, description):
	"""Recover the event segment the front door wrote as '<channel> <event>'."""
	prefix = f"{channel} "
	if description and description.startswith(prefix):
		return description[len(prefix):].strip() or None
	return None


