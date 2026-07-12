"""Shared inbound-webhook spine — one front door for every provider.

Every inbound webhook takes the same moves, in this order:

  authenticate (webhooks.ingress; token, optional HMAC, optional IP)
  -> kill-switch (default OFF) -> screen -> persist the raw payload ONCE, with the outcome
  -> fast 2xx ACK + enqueue.

The order is load-bearing. Authentication is first so that an unauthenticated flood can never write a
row, and the kill-switch is second so that a dormant integration still keeps an audit trail rather
than destroying what arrives. Screening before persisting is what makes the status truthful: it used
to persist Queued and only then decide, leaving every declined delivery parked at Queued for ever,
indistinguishable from one that was genuinely stuck. Deciding first costs the same single INSERT.

Frappe's own log doctype does the storage, and Frappe's own helpers do the writing — a webhook is an
Integration Request, and `frappe.integrations.utils.create_request_log` creates one. The status
vocabulary is that doctype's, not an invented one:

  Queued     enqueued; the worker will flip it.
  Completed  handled.
  Cancelled  deliberately not processed. The outcome says which gate declined it.
  Failed     the worker raised. The traceback is in `error`, so the DLQ is durable and queryable.

Retention is Frappe's too: `Integration Request` is registered in frappe's own
`default_log_clearing_doctypes` at 90 days, so nothing is registered here.

Adapter contract (duck-typed module, no ABC):
  * screen(payload, event, account)            -> (wanted, reason). Asked once, at the front door and
    again on replay. The reason is written onto a declined row so an operator can act on it.
  * already_processed(payload, event, account) -> idempotency vs the target doctype
  * handle(payload, event, account)            -> parse + DB moves + fail-closed attribution
  * account_for_payload(payload, event)        -> re-derive the account on replay and reconcile
"""
import frappe
from frappe import _
from frappe.integrations.utils import create_request_log

from tatva_connect.webhooks import ingress, registry

LOG_DOCTYPE = "Integration Request"

_GENERIC_DECLINE = "not accepted by this provider's pre-filter"


def receive(service, *, enabled, adapter, event=None):
	"""Shared front door. Authenticates, decides, logs once, then ACKs fast.

	Authentication comes FIRST, before the kill-switch. That ordering is deliberate: a dormant
	integration must still keep an audit trail of what arrived — a switch that is off means "do not
	act on this", not "destroy it" — but only an authenticated caller may write a row, or an
	unauthenticated flood could fill the log table.

	`enabled` is the provider's kill-switch callback; `adapter` is its module, passed in so the front
	door never imports one. Authentication is not a callback: it is the same three factors for every
	provider and lives in `webhooks.ingress`.
	"""
	account = ingress.verify(service)       # token digest -> HMAC -> IP; raises, fail-closed
	payload = _request_payload()            # form_dict minus cmd/token

	if not enabled():                       # kill-switch, fresh read, default OFF
		# Recorded, not discarded. Providers retry very few times — Acefone twice — so a call dropped
		# while the switch was off would be gone for good. Logged Cancelled, it is replayable the
		# moment the integration is turned on.
		_persist(service, event, payload, False, "the integration is switched off")
		return "ok"

	wanted, reason = _screen(adapter, payload, event, account)
	log = _persist(service, event, payload, wanted, reason)
	if not wanted:
		return "ok"

	frappe.enqueue(
		"tatva_connect.webhooks.spine.process",
		queue="short",
		service=service,
		payload=payload,
		account=account,
		# NB: 'event' is a RESERVED kwarg of frappe.enqueue (its queue-clearing arg) — passing it
		# here would bind to enqueue itself and never reach process(). Forward the provider's
		# sub-event under a non-reserved name.
		vendor_event=event,
		log=log,
	)
	return "ok"


def process(service, payload, account, vendor_event=None, log=None):
	"""Worker: dedupe, then hand to the adapter. Runs privileged — the front door is a guest
	endpoint, but persistence and downstream automation must run as a system user.

	A raised handler lands its traceback on the log row AND reaches the RQ failed registry, so the
	DLQ is durable in the database and replayable from the Desk. Nothing is lost behind a 200.
	"""
	if frappe.session.user == "Guest":
		frappe.set_user("Administrator")
	adapter = _adapter_for(service)
	try:
		if adapter.already_processed(payload, vendor_event, account):
			_mark(log, "Completed", output={"outcome": "already processed"})
			return
		adapter.handle(payload, vendor_event, account)
	except Exception:
		# Rolled back BEFORE the status is written. A half-finished handler must not have its partial
		# writes flushed by the very commit that records the failure — the row would then be replayed
		# over state it had already half-created.
		frappe.db.rollback()
		_mark(log, "Failed", error=frappe.get_traceback())
		raise
	_mark(log, "Completed")


# ---------------------------------------------------------------------------
# Outcome, decided before anything is written.
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


# ---------------------------------------------------------------------------
# The raw log. One row per delivery, written once, never raising.
# ---------------------------------------------------------------------------
def _persist(service, event, payload, relevant, reason):
	"""Persist the delivery as an Integration Request and return its name, or None.

	Never raises: durable logging must not break a webhook. The privileged insert happens inside
	Frappe's own `create_request_log`, which is why no `ignore_permissions` marker appears here.
	"""
	try:
		row = create_request_log(
			payload,
			service_name=service,
			request_description=f"{service} {event or ''}".strip(),
			status="Queued" if relevant else "Cancelled",
			# Empty strings, not None. The helper runs both through `frappe.as_json`, which turns a
			# None into the literal string "null" — which would then read as a populated Error field
			# on every row. A str is passed through untouched.
			output="" if relevant else frappe.as_json({"outcome": "not captured", "reason": reason}),
			error="",
			# Passed explicitly so the helper never reaches into the payload looking for one; a
			# provider is free to post a body carrying a `reference_doctype` key of its own.
			reference_doctype=None,
			reference_docname=None,
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
		# The raw log never got written, so there is nowhere to record this. A traceback still has to
		# survive somewhere the operator can find it.
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
def _adapter_for(service):
	"""The adapter module for a service, from the provider registry. Lazy-imported, so the spine
	stays import-safe and free of any provider import."""
	cfg = registry.by_service(service)
	if not cfg:
		frappe.throw(f"No webhook provider registered for service {service!r}")
	return frappe.get_module(cfg["adapter"])


def _request_payload():
	"""The provider's payload as a plain dict — form_dict minus Frappe's own keys."""
	return {k: v for k, v in frappe.form_dict.items() if k not in ("cmd", "token")}


# ---------------------------------------------------------------------------
# Replay — re-run the worker over stored raw payloads. System Manager only.
# ---------------------------------------------------------------------------
REPLAYABLE = ("Failed", "Cancelled")


@frappe.whitelist()
def replay(integration_request):
	"""Re-run one stored delivery.

	Replaying a Cancelled row is the point of the Cancelled status: map the DID the reason names,
	replay, and the call that was dropped lands.

	The delivery is re-screened first, against today's configuration. If it is still not wanted the
	row stays Cancelled and its reason is refreshed — a replay that did nothing must never be able to
	report success, or the act of trying to recover dropped calls would quietly destroy the list of
	them.
	"""
	frappe.only_for("System Manager")
	row = frappe.get_doc(LOG_DOCTYPE, integration_request)
	service = row.integration_request_service
	payload = frappe.parse_json(row.data) or {}
	event = _event_from_description(service, row.request_description)
	account = _account_for_replay(service, payload, event)

	wanted, reason = _screen(_adapter_for(service), payload, event, account)
	if not wanted:
		_mark(row.name, "Cancelled", output={"outcome": "not captured", "reason": reason})
		return reason

	process(service, payload, account, vendor_event=event, log=row.name)
	return "ok"


@frappe.whitelist()
def replay_service(service, status="Failed", since=None):
	"""Re-enqueue a service's replayable rows: Failed (the worker raised) or Cancelled (declined).

	Cancelled is the one an operator reaches for after fixing configuration — map a DID, then replay
	everything that was dropped for want of it. A row is Failed only once its worker actually raised,
	so this never re-runs still-in-flight work. The rows stay; each re-run flips its own status.
	"""
	frappe.only_for("System Manager")
	if status not in REPLAYABLE:
		frappe.throw(_("Only {0} deliveries can be replayed.").format(" or ".join(REPLAYABLE)))

	filters = {"integration_request_service": service, "status": status}
	if since:
		filters["creation"] = [">=", since]
	names = frappe.get_all(LOG_DOCTYPE, filters=filters, pluck="name")  # authz-ok: operator-only DLQ replay over system Integration Request rows, not user records
	for name in names:
		frappe.enqueue("tatva_connect.webhooks.spine.replay", queue="short", integration_request=name)
	return len(names)


def _event_from_description(service, description):
	"""Recover the event segment the front door wrote as '<service> <event>'."""
	prefix = f"{service} "
	if description and description.startswith(prefix):
		return description[len(prefix):].strip() or None
	return None


def _account_for_replay(service, payload, event):
	"""Re-derive the receiving account for a replayed payload.

	A replay carries the payload, not the live request token, so each adapter exposes how it recovers
	its account. A missing hook or a failed resolve returns None, and the adapter fails closed.
	"""
	adapter = _adapter_for(service)
	resolver = getattr(adapter, "account_for_payload", None)
	if not resolver:
		return None
	try:
		return resolver(payload, event)
	except Exception:
		return None
