"""WhatsApp persistence — vendor-blind. It reads a `ChannelEvent` and nothing else.

This is the half of the old `whatsapp/adapter.py` that was never WATI's: deciding which leads a
message belongs to, writing the `WhatsApp Message` rows, ticking a status, filing the media. None of
it depends on who carried the bytes, and all of it used to be interleaved with code that did — which
is why the vendor's field names (`waId`, `localMessageId`, `interactiveButtonReply`) reached as far as
the database.

An adapter normalizes; this applies. The one vendor-specific call left here is fetching media bytes,
which needs the account's own credentials — so it goes back out through the declared adapter surface
(`fetch_media`) rather than being re-implemented per provider.

Seams this does NOT own, and must never re-decide:
  * grain -> account routing lives in `whatsapp.routing` over the shared engine in `tatva_connect.routing`
  * File / Azure / privacy lives in `storage.file_manager`, reached through `whatsapp.media`
  * the front door, the raw log and the dedupe-dispatch live in `webhooks.spine`
"""
import frappe
from frappe import _

from tatva_connect.channels import resolve
from tatva_connect.channels.event import parse_timestamp
from tatva_connect.whatsapp import media as media_module
from tatva_connect.whatsapp import media_retry, routing

# The media kinds that carry bytes we download and file against the lead.
MEDIA_TYPES = media_module._MEDIA_TYPES


def _content_types() -> set:
	"""The `content_type` Select's real options, read from the doctype.

	Read rather than restated: a provider's own type vocabulary is NOT this field's vocabulary, and
	copying one across cost us every `templateMessageSent_v2` from the portal — "template" is not an
	option, the insert raised ValidationError, and the message was lost in the worker while the screen
	had already said it wanted it.
	"""
	meta = frappe.get_meta("WhatsApp Message")
	field = meta.get_field("content_type")
	return set((field.options or "").split("\n")) if field else set()


def content_type_for(value: str) -> str:
	"""A `content_type` this doctype will actually accept. Anything else lands as plain text — a bubble
	that renders is strictly better than an event that raises."""
	value = (value or "").strip()
	return value if value in _content_types() else "text"


def apply(event) -> None:
	"""Persist one normalized channel event. The single entry: the live webhook worker, the history
	backfill and orphan-status recovery all arrive here, so there is one brain for all three."""
	if not event:
		return
	if event.kind == "inbound":
		_ingest_inbound(event)
	elif event.kind == "outbound_echo":
		_ingest_outbound(event)
	elif event.kind == "status":
		_update_status(event)


def apply_historical(event) -> None:
	"""Persist a message we learned about AFTER the fact — a recovery, or a history backfill.

	It writes exactly what `apply` writes and deliberately differs in what it STARTS: a message pulled
	out of the past is not a live event, so it must open no journey and ring no rep's phone. Filing a
	three-week-old reply must not raise today's follow-up task.

	`in_workflow` is the codebase's own entry-trigger suppressor (`workflow_engine.triggers`), and the
	WhatsApp notification handler honours the same flag — so one existing flag covers both lanes and no
	second one is invented. Restored rather than cleared, so a nested caller keeps its own suppression.
	"""
	previous = frappe.flags.get("in_workflow")
	frappe.flags.in_workflow = True
	try:
		apply(event)
	finally:
		frappe.flags.in_workflow = previous


# ---------------------------------------------------------------------------
# Attribution — which leads own this conversation. One brain: whatsapp.routing.
# ---------------------------------------------------------------------------
def _targets(event, enrol_unknown=False):
	"""The leads this event belongs to: on this number AND routing to the receiving account.

	Strict by design. No hit -> DROP with a one-line log; never best-guess to a lead on another
	account, because that is a patient's conversation appearing under another programme.

	`enrol_unknown` lets a caller answer the drop differently: rather than losing the sender, mint the
	lead the conversation is about and file the message on it. Off for every caller but live inbound —
	an outbound echo means a number we messaged, and a campaign sent from the provider's own portal must
	never mint a lead per recipient. Dormant even there; see `enrol.lead_for_event` for both gates.
	"""
	number = event.subject_number
	if not number or not event.account:
		return []
	candidates = routing.candidates_for_number("+" + number)
	targets = routing.leads_for_number_and_account(candidates, event.account)
	if not targets and enrol_unknown and (lead := _enrol(event)):
		targets = [lead]
	if not targets:
		frappe.log_error(
			title=f"{event.channel} {event.kind} dropped: no lead routes to the receiving account",
			message=f"number={number} account={event.account} provider={event.provider}",
		)
	return targets


def _enrol(event):
	"""The lead minted for an unknown sender, or None — and never on a message out of the past.

	`in_workflow` is `apply_historical`'s own suppressor, already honoured by the notification handler
	for exactly this class of decision. A backfill re-reads months of conversations, and every stranger
	in them would otherwise be enrolled today as though they had just written in: hundreds of leads
	born at once, each firing assignment and whatever a Created flow does. Filing an old message must
	never look like a new patient.
	"""
	if frappe.flags.get("in_workflow"):
		return None
	from tatva_connect.whatsapp import enrol

	return enrol.lead_for_event(event)


def fetch_media(event):
	"""Download this event's media through the account's own adapter, or None.

	Two provider routes, one order of preference. The message-id route (`recover_media`) is asked first
	because the provider's message id is present on a webhook AND on a history item, so it is the one
	route that serves live ingest, backfill and recovery alike. It also returns the provider's own name
	for the file — which for a document is a uuid, so it is passed to `_filename_for` as a FALLBACK and
	never as the preferred name. The event's media URL is the fallback, and
	is all a webhook-only provider will ever have.

	Returns (content, filename) — never raises: a provider outage on the media endpoint must cost the
	attachment, not the message.
	"""
	if not (event.media_type in MEDIA_TYPES and event.account):
		return None
	if not (event.media_url or event.provider_message_id):
		return None
	try:
		account_doc = frappe.get_doc("WhatsApp Account", event.account)
		adapter = resolve.adapter_for(account_doc)
		if event.provider_message_id and adapter.DECLARATION.can("recover_media"):
			found = adapter.fetch_media_by_message_id(account_doc, event.provider_message_id)
			if found:
				return found[0], _filename_for(event, provider_name=found[1])
		if not event.media_url:
			return None
		content = adapter.fetch_media(account_doc, event.media_url)
	except Exception:
		frappe.log_error(
			title=f"{event.channel} media download failed",
			message=f"provider_message_id={event.provider_message_id} type={event.media_type}",
		)
		return None
	return content, _filename_for(event)


def _filename_for(event, provider_name=None) -> str:
	"""The filename to file this event's media under. `provider_name` is what the provider called it —
	a fallback, not a preference: WATI's own name for a document is a uuid, while the real one rides in
	the message body. `media_filename` owns that ordering for every route in."""
	return media_module.media_filename(
		event.media_type, event.filename, event.media_url or "", provider_name
	)


def _apply_media(doc, event, lead, media):
	"""Put the event's media on the row, or say plainly that it is not there.

	The placeholder fires on a FAILED DOWNLOAD, not on a blank body. Keying it on the body is what left
	a failed document as `content_type: document` with an empty `attach` — a bubble offering a file
	that does not exist — because a document's body is its filename and so was never blank.
	"""
	if media:
		content, filename = media
		filedoc = media_module.ensure_lead_media(lead, event.provider_message_id, filename, content)
		doc.content_type = content_type_for(event.media_type)
		doc.attach = filedoc.file_url          # proxy URL -> bubble renders; linker skips it (contract C)
		media_retry.settle(doc)
		# The body is already the event's text — an image's caption, a document's filename. Left alone.
		return
	if event.media_type in MEDIA_TYPES and (event.media_url or event.provider_message_id):
		doc.content_type = "text"
		doc.attach = None
		doc.message = (
			_("Media unavailable: {0}").format(event.filename) if event.filename else _("Media unavailable")
		)
		# Not a verdict — the row is OWED these bytes, and says so with what a later attempt needs to ask again.
		media_retry.park(doc, event)


def _stamp_provenance(doc, event) -> None:
	"""Correct the row's bookkeeping to the PROVIDER's truth, after the insert that guessed it.

	Frappe stamps `creation` and `owner` from the moment and the session that wrote the row
	(`document.set_user_and_timestamp`, which runs before any hook of ours and overwrites an
	assignment made earlier). That is right for a document a user authors and wrong for one we file on
	someone else's behalf. On the live webhook the guess is close enough to hide the difference; on a
	history backfill it is not — a three-week-old reply landed at today's time, in the middle of the
	thread, owned by whoever clicked Refresh, and the Activity rail then read "<rep> received a
	WhatsApp" on a patient's message.

	`creation` is the ONLY timestamp this doctype has — the chat tab both sorts and renders by it — so
	the provider's own stamp goes there. And an ingested row is filed by the system, never authored by
	a rep, so it is owned by Administrator on every path in.
	"""
	values = {"owner": "Administrator"}
	at = parse_timestamp(event.at)
	if at:  # a stamp we cannot read is never guessed at — the row keeps frappe's own time
		values["creation"] = at
	frappe.db.set_value("WhatsApp Message", doc.name, values, update_modified=False)


# ---------------------------------------------------------------------------
# Inbound — the customer wrote to us.
# ---------------------------------------------------------------------------
def _ingest_inbound(event) -> None:
	# Before the lead lookup: the wamid names the exact message we sent, which beats resolving a lead by the number a tap came from.
	_wake_workflow(event, rows_for_reply_context(event))
	targets = _targets(event, enrol_unknown=True)
	if not targets:
		return
	media = fetch_media(event)
	for lead in targets:
		_insert_inbound_row(event, lead, media)
	frappe.db.commit()
	_republish(targets)


def held_by_lead(lead, event) -> bool:
	"""Does `lead` already hold this message? THE dedupe answer — every path asks this one question.

	Keyed on the provider's own message id: the live webhook, the portal echo and the v3 history item
	all carry it, so it is the single identity that spans every route in. The correlation id is the
	other half of the same question — it is what a row WE sent stores.

	And on the wamid, because the provider's id is a DELIVERY identity and not a message one: WATI delivered one image twice under two ids 0.8s apart while the wamid stayed 1:1 across nine.

	Nothing else is consulted. Keying on the name meant a history row, which carries no wamid, had no
	key at all and inserted unconditionally; a composite guess on sender and text read two genuine
	replies in one thread as one message and dropped the second.
	"""
	wid = event.provider_message_id
	if wid and frappe.db.exists("WhatsApp Message", {"custom_provider_message_id": wid, "reference_name": lead}):
		return True
	wam = event.wamid
	if wam and frappe.db.exists("WhatsApp Message", {"message_id": wam, "reference_name": lead}):
		return True
	cid = event.correlation_id
	if cid and frappe.db.exists("WhatsApp Message", {"message_id": cid, "reference_name": lead}):
		return True
	return False


def held_by_account(account, provider_message_id) -> bool:
	"""Does this ACCOUNT hold this message, on any lead? The same key, asked before a provider fetch."""
	if not provider_message_id:
		return False
	return bool(
		frappe.db.exists(
			"WhatsApp Message",
			{"custom_provider_message_id": provider_message_id, "whatsapp_account": account},
		)
	)


def _insert_inbound_row(event, lead, media) -> None:
	"""Insert one inbound row for `lead`, idempotent on the provider's message id.

	The name stays scoped per-lead ({lead}-{wamid}) so one message mirrors onto more than one
	same-account lead without colliding on the PK — but it is a naming scheme now, not the dedupe key.
	"""
	if held_by_lead(lead, event):
		return
	name = f"{lead}-{event.wamid}" if event.wamid else None
	doc = frappe.get_doc(
		{
			"doctype": "WhatsApp Message",
			"type": "Incoming",
			"from": event.subject_number,
			"message": event.text or "",
			"content_type": content_type_for(event.media_type),
			"message_id": event.wamid,
			"custom_provider_message_id": event.provider_message_id,  # cross-path identity (live + history backfill)
			"conversation_id": event.conversation_id,
			"profile_name": event.raw.get("senderName") if isinstance(event.raw, dict) else None,
			"whatsapp_account": event.account,
			"reference_doctype": "CRM Lead",
			"reference_name": lead,
		}
	)
	_apply_media(doc, event, lead, media)
	# An interactive reply's machine-readable identity. The title is what the human saw and the id is what an automation keys on; storing only WATI's echo of the title into `text` meant a rule could only ever match on prose a marketer is free to reword.
	if event.button_id:
		doc.custom_button_id = event.button_id
	if event.button_title:
		doc.custom_button_title = event.button_title
	if event.reply_to:
		# The doctype's own reply pair — the fork's chat tab already renders it. No second field.
		doc.is_reply = 1
		doc.reply_to_message_id = event.reply_to
	if name:
		doc.name = name
		doc.flags.name_set = True
	doc.flags.tatva_pinned_lead = lead  # restored in before_save (see pin_inbound_reference)
	doc.insert(ignore_permissions=True)  # authz-ok: tier-b — webhook: token-authenticated + phone+account attribution
	_stamp_provenance(doc, event)


# ---------------------------------------------------------------------------
# Outbound echo — a message that left the business outside this CRM.
# ---------------------------------------------------------------------------
def _ingest_outbound(event) -> None:
	"""Mirror a message an agent or bot sent from the provider's own portal.

	If a row already carries this correlation id then WE sent it, and this event is a delivery-status
	confirmation rather than a new message — so it goes to the status path, not a duplicate insert.
	"""
	if event.correlation_id and rows_for_correlation(event):
		_update_status(event)
		return
	targets = _targets(event)
	if not targets:
		return
	media = fetch_media(event)
	for lead in targets:
		_insert_outbound_row(event, lead, media)
	frappe.db.commit()
	_republish(targets)


def _insert_outbound_row(event, lead, media) -> None:
	"""Insert one Outgoing row mirroring a provider-side message onto `lead`. Per-lead idempotent on
	the provider's own message id — the ONE key shared by the live webhook and the history backfill, so
	the two paths never double-insert the same message."""
	wid = event.provider_message_id
	if not wid:
		return
	if held_by_lead(lead, event):
		return
	name = f"{lead}-{wid}"

	# The sent event already carries the fully-rendered body (variables substituted), so it is stored as a plain Manual bubble — we are mirroring a message, not re-sending a template, and "Template" without a `template` link renders blank.
	doc = frappe.get_doc(
		{
			"doctype": "WhatsApp Message",
			"type": "Outgoing",
			"to": event.subject_number,
			"message": event.text or "",
			"content_type": content_type_for(event.media_type),
			"message_type": "Manual",
			"message_id": event.correlation_id,  # lets later delivered/read events tick this row
			"custom_provider_message_id": wid,
			"conversation_id": event.conversation_id,
			"profile_name": event.raw.get("operatorName") if isinstance(event.raw, dict) else None,
			"status": event.outcome,
			"whatsapp_account": event.account,
			"reference_doctype": "CRM Lead",
			"reference_name": lead,
		}
	)
	_apply_media(doc, event, lead, media)
	doc.name = name
	doc.flags.name_set = True
	doc.flags.tatva_ingested = True  # a mirror of an existing message — the controller must not re-send
	doc.insert(ignore_permissions=True)  # authz-ok: tier-b — webhook: token-authenticated + phone+account attribution
	_stamp_provenance(doc, event)


# ---------------------------------------------------------------------------
# Status — the provider reporting on a message already on the wire.
# ---------------------------------------------------------------------------
def rows_for_correlation(event):
	"""Every row this status is about, SCOPED TO THE RECEIVING ACCOUNT.

	The account scope is not decoration. Correlation ids are minted per tenant, and without the scope a
	status delivered on account B ticked a row sent on account A — one programme's delivery receipt
	painted onto another programme's message.

	A shared number can mirror one message onto more than one lead, so this is deliberately every
	matching row rather than the first.
	"""
	if not event.correlation_id:
		return []
	filters = {"message_id": event.correlation_id}
	if event.account:
		filters["whatsapp_account"] = event.account
	return frappe.get_all("WhatsApp Message", filters=filters, pluck="name")


def _waitable(event) -> str | None:
	"""The workflow signal name for this outcome, or None when nothing may wait on it.

	Asked of the ONE declaration a Wait's picker is built from (`registry.outcomes_for`), so the events
	this bridge delivers and the events an author can select are the same set by construction rather than
	by two lists agreeing. `sent`/`failed` are excluded there because the send path already returned them
	synchronously - which is also why no `sent` status is reported unmappable below, since a `sent` can
	legitimately arrive before the row it would correlate through is committed.
	"""
	from tatva_connect.workflow_engine import registry

	name = f"{event.channel}.{event.outcome}"
	return name if name in registry.outcomes_for("Send WhatsApp") else None


def _wake_workflow(event, rows) -> None:
	"""Wake the run that sent THIS message - never another run parked on the same lead.

	The provider's id and the engine's own `run::node` token meet on the `WhatsApp Message` row: the send
	job wrote both, this reads them back. Correlating on the lead alone would wake whichever run answered
	first, which is the defect this exists to prevent when one lead is in two journeys.

	A row with no token was not sent by a workflow (a rep's manual send, a notification) and is simply not
	ours to wake - that is silence, not a loss.
	"""
	signal = _waitable(event)
	if not signal:
		return

	from tatva_connect.workflow_engine import signals

	for row in rows:
		record = frappe.db.get_value(
			"WhatsApp Message", row, ["custom_workflow_correlation", "reference_doctype", "reference_name"],
			as_dict=True,
		)
		if not record or not record.custom_workflow_correlation or record.reference_doctype != "CRM Lead":
			continue
		signals.deliver_signal(
			"CRM Lead", record.reference_name, signal,
			correlation=record.custom_workflow_correlation,
			payload={
				"outcome": event.outcome, "message_id": event.correlation_id,
				# WHICH button, machine-readable. The title is display text a marketer may reword.
				"button_id": event.button_id, "button_title": event.button_title,
			},
		)


def rows_for_reply_context(event):
	"""The outbound row an inbound tap points BACK at, joined on the wamid the send stored.

	A button tap carries `replyContextId` = the wamid of the message that offered the buttons. That is a
	different identity from the `localMessageId` the status path joins on, which is why the send stores
	both: only the correlation id is echoed by status events, and only the wamid is what a tap references.

	Account-scoped for the same reason `rows_for_correlation` is: ids are minted per tenant, and a tap
	delivered on account B must not reach a message sent on account A.
	"""
	if not event.reply_to:
		return []
	filters = {"custom_outbound_wamid": event.reply_to}
	if event.account:
		filters["whatsapp_account"] = event.account
	return frappe.get_all("WhatsApp Message", filters=filters, pluck="name")


def _update_status(event) -> None:
	if not event.outcome:
		return
	rows = rows_for_correlation(event)
	if not rows:
		# A status we cannot place. Never dropped in silence: a run may be parked waiting for exactly this, and the operator's only clue would be a journey that stopped. `sent` cannot reach here (see `_waitable`), so this stays quiet in normal traffic.
		if _waitable(event):
			frappe.log_error(
				title="whatsapp: delivery status matches no message",
				message=f"outcome={event.outcome} correlation_id={event.correlation_id} account={event.account}",
			)
		return
	values = {"status": event.outcome}
	# A failure with no reason is an operator staring at the word "failed" with nowhere to go. The provider sent both halves; store them.
	if event.outcome == "failed" and (event.error_code or event.error_detail):
		values["custom_failed_reason"] = " · ".join(
			str(part) for part in (event.error_code, event.error_detail) if part
		)
	for row in rows:
		frappe.db.set_value("WhatsApp Message", row, values, update_modified=False)
	frappe.db.commit()
	# After the commit: the signal enqueues a resume that re-reads this row, so it must find it written.
	_wake_workflow(event, rows)


def _republish(leads) -> None:
	"""Re-emit crm's realtime event AFTER the commit.

	crm publishes "whatsapp_message" in its on_update hook, which fires DURING insert and BEFORE the
	commit — a browser reloading on that event reads the row before it is committed, so the chat tab
	lags one message behind. Re-emitting here makes the reload fetch committed data.
	"""
	for lead in leads:
		frappe.publish_realtime("whatsapp_message", {"reference_doctype": "CRM Lead", "reference_name": lead})
