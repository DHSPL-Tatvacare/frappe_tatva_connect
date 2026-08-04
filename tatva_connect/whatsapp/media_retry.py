# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Media this channel is still owed — parked with its pointer, and spent down by a sweep.

THE DEFECT. A failed download wrote `Media unavailable` and the row was final. A provider blip of two
minutes degraded that message for ever: nothing distinguished *the fetch failed*, *the provider has not
published it yet* and *there is no media*, and nothing ever tried again. `contract.RecordingRef` names
that collapse as the bug it exists to prevent, and telephony has not had it since.

WHAT IS SHARED WITH `storage.call_media` IS THE DISCIPLINE, NOT THE TABLE — three distinguishable
outcomes, backoff, a spend budget, a dormant switch and per-row commit. The table is deliberately not
shared: a recording is one call's, while WhatsApp media is fetched once and STORED PER LEAD
(`media.ensure_lead_media`), so each row's outcome is genuinely its own and belongs on the row.

NO VENDOR IS NAMED HERE. The retry re-asks through `resolve.adapter_for` and the adapter's own
declaration, exactly as the live path does, so a second provider that declares `recover_media` inherits
this with no code. What a provider CAN recover is its own truth: an adapter holding only a webhook media
URL retries against that URL's lifetime, and one that can read by message id is durable.

THE HAPPY PATH IS UNCHANGED. The first fetch is still inline and still synchronous, because the bubble
renders on arrival. `Media unavailable` stops being a verdict and becomes what the bubble says WHILE
awaiting.
"""
import frappe
from frappe.utils import now_datetime

from tatva_connect import automation
from tatva_connect.channels import retry
from tatva_connect.whatsapp import channel

MESSAGE_DT = "WhatsApp Message"

# The ladder, the budget and the transition are `channels.retry`'s — this ledger owns only WHERE the state is written. Blank is the state this ledger adds and the commonest: the message never carried media at all.
AWAITING = retry.AWAITING
STORED = retry.STORED
ABANDONED = retry.ABANDONED

# Rows one pass claims. A backlog drains over several passes rather than one job holding a transaction.
SWEEP_BATCH = 100


def park(doc, event) -> None:
	"""Record that this row is owed media, with what a later attempt needs to ask again.

	Called on the row IN MEMORY before insert, so parking costs no second write. The media type and the
	provider's reference are kept because `_apply_media` rewrites `content_type` to text on a failure —
	without them the retry would not know what it was fetching or where from.
	"""
	doc.custom_media_state = AWAITING
	doc.custom_media_type = event.media_type
	doc.custom_media_ref = event.media_url
	doc.custom_media_attempts = 0
	doc.custom_media_next_attempt_at = retry.first_attempt_at()


def settle(doc) -> None:
	"""Media arrived on the first try — say so, so the sweep never reads this row."""
	doc.custom_media_state = STORED


def sweep() -> int:
	"""The scheduled tick: re-ask for every row whose next attempt has come. Gated on the media-retry switch."""
	if not automation.is_enabled(channel.SWITCH_MEDIA_RETRY):
		return 0
	recovered = 0
	for row in _due_rows():
		if _retry(row):
			recovered += 1
		frappe.db.commit()  # per row, so a worker killed mid-sweep never re-fetches what it already stored
	return recovered


def _due_rows():
	"""Awaiting rows whose next attempt has come, oldest first, capped. A row with no next attempt is not due by definition, so NULL semantics keep an abandoned row out without a second flag."""
	return frappe.get_all(  # authz-ok: tier-a — scheduler context, artifact state on system rows
		MESSAGE_DT,
		filters={
			"custom_media_state": AWAITING,
			"custom_media_next_attempt_at": ["<=", now_datetime()],
		},
		fields=[
			"name", "custom_provider_message_id", "custom_media_type", "custom_media_ref",
			"custom_media_attempts", "whatsapp_account", "reference_name",
		],
		order_by="custom_media_next_attempt_at asc",
		limit=SWEEP_BATCH,
	)


def _retry(row) -> bool:
	"""One row's next attempt. Never raises — a row that cannot be recovered spends an attempt and the sweep moves on."""
	from tatva_connect.whatsapp import ingest, media

	try:
		found = ingest.fetch_media(_ask(row))
		if not found:
			_spend(row)
			return False
		content, filename = found
		filedoc = media.ensure_lead_media(row.reference_name, row.custom_provider_message_id, filename, content)
		frappe.db.set_value(MESSAGE_DT, row.name, {
			"attach": filedoc.file_url,
			"content_type": ingest.content_type_for(row.custom_media_type),
			"message": "",
			"custom_media_state": STORED,
			"custom_media_next_attempt_at": None,
		}, update_modified=False)
	except Exception:
		frappe.db.rollback()
		frappe.log_error(title="WhatsApp media retry failed", message=frappe.get_traceback())
		_spend(row)
		return False
	# The bubble said "Media unavailable" until this moment; the reader is told it changed.
	frappe.publish_realtime(
		"whatsapp_message", {"reference_doctype": "CRM Lead", "reference_name": row.reference_name}
	)
	return True


def _ask(row):
	"""What `ingest.fetch_media` reads, rebuilt from the row. THE attribute set is the contract between the two — a fetcher that starts reading a sixth field will find it missing here, loudly, rather than silently fetching nothing."""
	return frappe._dict(
		channel="whatsapp",
		account=row.whatsapp_account,
		media_type=row.custom_media_type,
		media_url=row.custom_media_ref,
		provider_message_id=row.custom_provider_message_id,
		filename=None,
	)


def _spend(row) -> None:
	"""Spend one attempt. The budget's end is ABANDONED, which is terminal — a redelivery does not buy another."""
	attempts = (row.custom_media_attempts or 0) + 1
	state, next_at = retry.spend(attempts)
	frappe.db.set_value(MESSAGE_DT, row.name, {
		"custom_media_state": state,
		"custom_media_attempts": attempts,
		"custom_media_next_attempt_at": next_at,
	}, update_modified=False)
