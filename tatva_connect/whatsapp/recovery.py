"""Orphan-status recovery — the message a status names but this CRM never stored.

A delivered/read/failed event used to be dropped when no local row carried its correlation id, and
with it went the only proof that the message existed at all. It always could have been recovered: a
status carries the provider's `conversationId` and its message `id` on 100% of live traffic. What it
never carries is a phone number — `waId` and `bsuid` are absent from every status event measured — so
recovery keys on the conversation, and a design that needed a number could not have worked.

Four properties, each of which is a rule and not an aspiration:

  SURGICAL   one message is pulled from the conversation and inserted. Never a thread, never a
             flush-and-refill — a status event is not authority to rewrite a conversation.
  ATTRIBUTED from OUR data. A v3 message item carries no contact identifier at all, so the provider
             cannot tell us whose thread this is — the conversation's existing rows do. A conversation
             we hold nothing on cannot be attributed, and is dropped with that said out loud.
  BACKFILL   the insert goes through `ingest.apply_historical`, so a message we are learning about
             after the fact starts no journey and pings nobody's phone.
  IDEMPOTENT a message already held is not inserted again; the status is simply applied. Running this
             twice leaves exactly one row.
  FAIL SOFT  a recovery that fails logs why and changes nothing else. The status was already declined
             by the screen, so the ingest path is identical whether this works or not.

Dormant by default behind `WhatsApp::Channel::recovery`, and only for a provider that DECLARES
`recover_message`. One that does not simply keeps today's behaviour: the status is logged and dropped.
"""
import frappe

from tatva_connect import automation, phone
from tatva_connect.channels import resolve
from tatva_connect.whatsapp import channel, ingest

ACCOUNT_DOCTYPE = "WhatsApp Account"

# What the declined delivery's log row says. Recovery only ever adds to it — the status is declined either way, and an operator reading the row must still see the original reason.
_DECLINED = "a status update for a message this CRM did not send"


def queue(event) -> str:
	"""Enqueue the recovery for one orphan status; return the reason recorded against it.

	Called from the adapter's `handle`, in the worker — never from `screen`. Screening decides whether a
	delivery is wanted and must not act, or a DLQ replay would re-screen the same declined status and
	fire a fresh provider fetch every time. This only ever reaches Redis, never the provider, so a slow
	or dead WATI delays nothing. It never raises: a bug in recovery must not change what the ingest path
	does with a status.
	"""
	try:
		if not event or not event.account:
			return _DECLINED
		if not automation.is_enabled(channel.SWITCH_RECOVERY):
			return f"{_DECLINED}; recovery is switched off"
		if not (event.conversation_id and event.provider_message_id):
			return f"{_DECLINED}; it names no conversation to recover it from"
		if not resolve.adapter_for(event.account, ACCOUNT_DOCTYPE).DECLARATION.can("recover_message"):
			return f"{_DECLINED}; this provider cannot recover it"
		frappe.enqueue(
			"tatva_connect.whatsapp.recovery.recover",
			queue="short",
			# A provider re-sends a status, and each copy screens to the same recovery. Keyed on the message being recovered, the copies collapse into one job.
			job_id=f"whatsapp-recover:{event.account}:{event.provider_message_id}",
			deduplicate=True,
			account=event.account,
			payload=event.raw,
		)
		return f"{_DECLINED}; recovery queued"
	except Exception:
		frappe.log_error(title="WhatsApp recovery could not be queued", message=frappe.get_traceback())
		return _DECLINED


def recover(account, payload) -> None:
	"""Worker: pull the ONE message this status names, file it as backfill, then apply the status.

	Runs privileged — the front door is a guest endpoint, but persistence must run as a system user.

	Rolls back BEFORE logging a failure and commits after: `frappe.log_error` inside a rolled-back
	transaction dies with it, and at that point the log row is the only pending write. The pattern is
	`observability/capture.py`'s, for the same reason.
	"""
	if frappe.session.user == "Guest":
		frappe.set_user("Administrator")
	try:
		_recover(account, payload)
	except Exception:
		frappe.db.rollback()
		frappe.log_error(title="WhatsApp recovery failed", message=frappe.get_traceback())
		frappe.db.commit()


def _recover(account, payload) -> None:
	"""Recover one message, then apply its status. Split out so `recover` holds only the failure rule."""
	adapter = resolve.adapter_for(account, ACCOUNT_DOCTYPE)
	status = adapter.normalize(payload, account)
	if not status or status.kind != "status" or not status.provider_message_id:
		return
	if not ingest.held_by_account(account, status.provider_message_id):
		if not _recover_one(adapter, account, status):
			return
	# Applied whether the message was just recovered or was already held — that is what makes running this twice a no-op rather than a second insert.
	ingest.apply(status)


def _recover_one(adapter, account, status) -> bool:
	"""Fetch and file the ONE message this status names. False when it could not be, having said why."""
	number = _number_for_conversation(account, status.conversation_id)
	if not number:
		# The accepted limit, named. A v3 item carries no contact identifier, so a conversation we hold no row for cannot be attributed to a lead by us OR by the provider — and a message filed against the wrong patient is far worse than one not filed at all. Logged, not guessed.
		frappe.log_error(
			title="WhatsApp recovery cannot attribute an unknown conversation",
			message=(
				f"account={account} conversation={status.conversation_id} "
				f"provider_message_id={status.provider_message_id} — this CRM holds no message on this "
				f"conversation, and a v3 message item carries no contact identifier to fall back to."
			),
		)
		return False
	account_doc = frappe.get_doc(ACCOUNT_DOCTYPE, account)
	item = adapter.recover_message(account_doc, status.conversation_id, status.provider_message_id)
	if not item:
		# The provider knows nothing of the message its own status named. Say so plainly — a silent return here would read as a successful recovery that inserted nothing.
		frappe.log_error(
			title="WhatsApp recovery found no such message",
			message=(
				f"account={account} conversation={status.conversation_id} "
				f"provider_message_id={status.provider_message_id}"
			),
		)
		return False
	# The two things the item genuinely does not carry, supplied from what WE hold: the subject number (from the conversation's own rows) and the correlation id (from the status that triggered this).
	ingest.apply_historical(
		adapter.normalize_history(
			item, account=account, number=number, correlation_id=status.correlation_id
		)
	)
	return True


def _number_for_conversation(account, conversation_id):
	"""The contact number this conversation belongs to, read from a message we ALREADY hold on it.

	This is the whole attribution brain for a recovered message, and it is deliberately ours rather than
	the provider's: a v3 message item carries no contact identifier of any kind (measured over a 100-item
	field union — no wa_id, phone, contact_id or bsuid), while a status event carries `conversationId` on
	100% of live traffic and every row we write stores the conversation it belongs to.

	One conversation is one contact's whole thread, so any row on it names the same number. Returning the
	NUMBER rather than a lead is what keeps this a lookup instead of a second attribution engine: the
	number goes back through `ingest`'s own `_targets`, so account routing and shared-number mirroring
	keep working exactly as they do for a live message.
	"""
	if not conversation_id:
		return None
	row = frappe.db.get_value(
		"WhatsApp Message",
		{"conversation_id": conversation_id, "whatsapp_account": account},
		["`from`", "`to`"],
		as_dict=True,
	)
	if not row:
		return None
	# An Incoming row names the contact in `from`, an Outgoing one in `to`. Either identifies the thread.
	return phone.match_digits(row.get("from") or row.get("to")) or None


