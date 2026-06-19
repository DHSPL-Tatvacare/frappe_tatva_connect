"""Shared inbound-webhook spine — ONE front door for every vendor (WATI, Acefone, …).

Every inbound webhook flows through the same five moves (the 8-pillar hardening
standard, `03-integrations/08-webhook-ingress-hardening-standard.md`):

  kill-switch (default OFF) -> authenticate+scope account (const-time, fail-closed)
  -> persist RAW (always-on Integration Request) -> cheap optimistic pre-filter
  -> fast 2xx ACK + enqueue.

The worker (`process`) dedupes then hands to the per-vendor adapter — the only
place vendor-specific payload parsing and DB moves live. The spine is identical
for all integrations; only the adapter differs. Reuses Frappe infra throughout:
`frappe.enqueue` (queue) · RQ failed registry (DLQ) · Integration Request
(raw-log sink, log-cleaner-purged at 90d). No new doctype.

Adapter contract (duck-typed module, no ABC):
  * is_relevant(payload, event, account) -> bool      — cheap front-door pre-filter
  * already_processed(payload, event, account) -> bool — idempotency vs the target doctype
  * handle(payload, event, account) -> None            — parse + clean DB moves + fail-closed attribution
"""
import json

import frappe

from tatva_connect.webhooks import registry


def receive(service, *, enabled, resolve_account, adapter, event=None):
	"""Shared front door. Authenticates, raw-logs, optimistically pre-filters, then
	enqueues the worker and returns 'ok' fast (< 5s, before any heavy work).

	`enabled`/`resolve_account` are vendor callbacks; `adapter` is the vendor module
	(passed in so the front door doesn't import it — the worker resolves it lazily)."""
	if not enabled():                       # kill-switch, fresh read, default OFF
		return "ok"
	token = _request_token()                # nginx maps /.../<token> -> ?token=
	account = resolve_account(token)        # auth + scope in one (fail-closed)
	if not account:
		raise frappe.PermissionError(f"Invalid {service} webhook token")
	payload = _request_payload()            # form_dict minus cmd/token
	log = _persist_raw(service, event, payload, account)   # ALWAYS-ON Integration Request
	if not adapter.is_relevant(payload, event, account):   # cheap optimistic pre-filter
		return "ok"
	frappe.enqueue(
		"tatva_connect.webhooks.spine.process",
		queue="short",
		service=service,
		payload=payload,
		account=account,
		# NB: 'event' is a RESERVED kwarg of frappe.enqueue (its queue-clearing arg) — passing
		# it here would bind to enqueue itself and never reach process(). Forward the vendor
		# sub-event under a non-reserved name.
		vendor_event=event,
		log=log,
	)
	return "ok"


def process(service, payload, account, vendor_event=None, log=None):
	"""Worker: dedupe then hand to the adapter. Runs privileged (the front door is a
	guest endpoint, but persistence + downstream automation must run as a system user).
	Exceptions propagate -> RQ failed registry (the DLQ) -> replayable; the always-on
	raw log already holds the payload, so nothing is ever lost behind a 200.

	`vendor_event` is the vendor sub-event (Acefone trigger; None for WATI) — named to dodge
	enqueue's reserved `event` kwarg; it maps to the adapter contract's `event` argument."""
	if frappe.session.user == "Guest":
		frappe.set_user("Administrator")
	adapter = _adapter_for(service)
	try:
		if adapter.already_processed(payload, vendor_event, account):
			_mark_log(log, "Completed")
			return
		adapter.handle(payload, vendor_event, account)
	except Exception:
		_mark_log(log, "Failed")   # truthful DLQ row, then let it reach the RQ failed registry
		raise
	_mark_log(log, "Completed")


# ---------------------------------------------------------------------------
# Raw log — always-on Integration Request (P6). Never raises.
# ---------------------------------------------------------------------------
def _persist_raw(service, event, payload, account):
	"""Write an always-on Integration Request capturing the raw payload, service-tagged,
	status 'Queued' (the worker flips it to Completed/Failed). Returns its name, or None.
	NEVER raises — durable logging must not break the webhook."""
	try:
		doc = frappe.get_doc(
			{
				"doctype": "Integration Request",
				"integration_request_service": service,
				"request_description": f"{service} {event or ''}".strip(),
				"status": "Queued",
				"data": json.dumps(payload, default=str),
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		return doc.name
	except Exception:
		frappe.db.rollback()
		return None


def _mark_log(log, status):
	"""Flip the raw-log Integration Request status. Never raises (best-effort bookkeeping)."""
	if not log:
		return
	try:
		frappe.db.set_value("Integration Request", log, "status", status, update_modified=False)
	except Exception:
		pass


# ---------------------------------------------------------------------------
# Adapter resolution (lazy import -> no import cycle; import-safe before adapters exist)
# ---------------------------------------------------------------------------
def _adapter_for(service):
	"""Return the adapter module for a service, from the provider registry. Lazy import
	(frappe.get_module) keeps the spine import-safe and free of any provider import."""
	cfg = registry.by_service(service)
	if not cfg:
		frappe.throw(f"No webhook provider registered for service {service!r}")
	return frappe.get_module(cfg["adapter"])


# ---------------------------------------------------------------------------
# Request helpers — mirror the current WATI/Acefone handlers exactly.
# ---------------------------------------------------------------------------
def _request_token():
	"""The receiving account's secret, carried in the URL: nginx maps
	/.../<token> -> ?token=<token>. For a JSON POST the body is in form_dict, so read
	the token from request.args (the query) first, then fall back to form_dict."""
	token = frappe.request.args.get("token") if frappe.request else None
	return token or frappe.form_dict.get("token")


def _request_payload():
	"""The vendor payload as a plain dict — form_dict minus Frappe/query keys."""
	return {k: v for k, v in frappe.form_dict.items() if k not in ("cmd", "token")}


# ---------------------------------------------------------------------------
# Replay (P6) — re-run the worker over stored raw payloads. System Manager only.
# ---------------------------------------------------------------------------
@frappe.whitelist()
def replay(integration_request):
	"""Re-run process() over one stored raw Integration Request. The row's
	`integration_request_service` selects the adapter; `request_description` carries the
	'<service> <event>' the front door wrote, so the event is recovered. Account is
	re-resolved per adapter at handle time from the payload — replay carries the payload,
	not the live request token."""
	row = frappe.get_doc("Integration Request", integration_request)
	service = row.integration_request_service
	payload = json.loads(row.data or "{}")
	event = _event_from_description(service, row.request_description)
	account = _account_for_replay(service, payload, event)
	process(service, payload, account, vendor_event=event, log=row.name)
	return "ok"


@frappe.whitelist()
def replay_failed(service, since=None):
	"""Re-enqueue every Failed raw Integration Request for a service (optionally
	only those created on/after `since`). Returns the count re-enqueued. The original
	rows stay; each re-run flips its own status. A truthful DLQ: a row is 'Failed' only
	when its worker actually raised (M1), so replay never re-runs still-Queued/in-flight work."""
	filters = {
		"integration_request_service": service,
		"status": "Failed",
	}
	if since:
		filters["creation"] = [">=", since]
	names = frappe.get_all("Integration Request", filters=filters, pluck="name")
	for name in names:
		frappe.enqueue(
			"tatva_connect.webhooks.spine.replay",
			queue="short",
			integration_request=name,
		)
	return len(names)


def _event_from_description(service, description):
	"""Recover the event segment the front door wrote as '<service> <event>'."""
	prefix = f"{service} "
	if description and description.startswith(prefix):
		return description[len(prefix):].strip() or None
	return None


def _account_for_replay(service, payload, event):
	"""Re-resolve the receiving account for a replayed payload. Replay has no live request
	token, so each adapter must expose how it re-derives its account from the stored
	payload. Defensive: a missing hook / failed resolve -> None (the adapter fails closed)."""
	adapter = _adapter_for(service)
	resolver = getattr(adapter, "account_for_payload", None)
	if not resolver:
		return None
	try:
		return resolver(payload, event)
	except Exception:
		return None
