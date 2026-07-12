"""Shared inbound-webhook spine — one front door for every provider.

Every inbound webhook takes the same five moves:

  kill-switch (default OFF) -> authenticate (webhooks.ingress; token, optional HMAC, optional IP)
  -> decide the outcome -> persist the raw payload ONCE, with that outcome
  -> fast 2xx ACK + enqueue.

The order matters. Persisting first and deciding second is what left every ignored call sitting at
`Queued` for ever, indistinguishable from one that was genuinely stuck. Deciding first costs the same
single INSERT and makes the status truthful.

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
  * is_relevant(payload, event, account)        -> cheap front-door pre-filter
  * already_processed(payload, event, account)  -> idempotency vs the target doctype
  * handle(payload, event, account)             -> parse + DB moves + fail-closed attribution
  * account_for_payload(payload, event)         -> re-derive the account on replay
  * irrelevance_reason(payload, event, account) -> OPTIONAL. Why a call was declined, in one line,
    for the operator reading the log. Cold path only; an adapter that omits it just reads generic.
"""
import frappe
from frappe.integrations.utils import create_request_log

from tatva_connect.webhooks import ingress, registry

LOG_DOCTYPE = "Integration Request"

_GENERIC_DECLINE = "not accepted by this provider's pre-filter"


def receive(service, *, enabled, adapter, event=None):
	"""Shared front door. Authenticates, decides, logs once, then ACKs fast.

	`enabled` is the provider's kill-switch callback; `adapter` is its module, passed in so the front
	door never imports one. Authentication is deliberately not a callback: it is the same three
	factors for every provider and lives in `webhooks.ingress`.
	"""
	if not enabled():                       # kill-switch, fresh read, default OFF
		return "ok"
	account = ingress.verify(service)       # token digest -> HMAC -> IP; raises, fail-closed
	payload = _request_payload()            # form_dict minus cmd/token

	relevant, reason = _verdict(adapter, payload, event, account)
	log = _persist(service, event, payload, relevant, reason)
	if not relevant:
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
		_mark(log, "Failed", error=frappe.get_traceback())
		raise
	_mark(log, "Completed")


# ---------------------------------------------------------------------------
# Outcome, decided before anything is written.
# ---------------------------------------------------------------------------
def _verdict(adapter, payload, event, account):
	"""(relevant, reason). A raising pre-filter is treated as relevant so the call is never dropped
	on a bug — the worker will surface the failure properly, with a traceback."""
	try:
		if adapter.is_relevant(payload, event, account):
			return True, None
	except Exception:
		frappe.log_error(title="webhook spine: pre-filter raised", message=frappe.get_traceback())
		return True, None
	return False, _irrelevance_reason(adapter, payload, event, account)


def _irrelevance_reason(adapter, payload, event, account):
	"""Why a call was declined, for the operator reading the log. Optional per adapter.

	Coerced to a string and never allowed to raise: an adapter that misbehaves here must not be able
	to break the logging that would have recorded its misbehaviour.
	"""
	describe = getattr(adapter, "irrelevance_reason", None)
	if not describe:
		return _GENERIC_DECLINE
	try:
		reason = describe(payload, event, account)
	except Exception:
		frappe.log_error(title="webhook spine: irrelevance_reason raised", message=frappe.get_traceback())
		return _GENERIC_DECLINE
	return str(reason) if reason else _GENERIC_DECLINE


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

	`IntegrationRequest.handle_success/handle_failure` do this natively, but through `db_set`, which
	bumps `modified`; the doctype carries `track_changes`, so that writes a Version row per webhook.
	On a table taking thousands of deliveries a day that cost is not worth paying, so the same fields
	are set through `frappe.db.set_value` with the timestamp left alone.
	"""
	if not log:
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
@frappe.whitelist()
def replay(integration_request):
	"""Re-run process() over one stored raw payload.

	The row's service selects the adapter and its description carries the event the front door wrote.
	The account is re-derived from the payload by the adapter, because a replay has no live request
	token. Replaying a Cancelled row is the point of the Cancelled status: map a DID, replay, and the
	call that was dropped lands.
	"""
	frappe.only_for("System Manager")
	row = frappe.get_doc(LOG_DOCTYPE, integration_request)
	service = row.integration_request_service
	payload = frappe.parse_json(row.data) or {}
	event = _event_from_description(service, row.request_description)
	account = _account_for_replay(service, payload, event)
	process(service, payload, account, vendor_event=event, log=row.name)
	return "ok"


@frappe.whitelist()
def replay_failed(service, since=None):
	"""Re-enqueue every Failed row for a service, optionally only those since a timestamp.

	A row is Failed only when its worker actually raised, so replay never re-runs still-in-flight
	work. The rows stay; each re-run flips its own status.
	"""
	frappe.only_for("System Manager")
	filters = {"integration_request_service": service, "status": "Failed"}
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
