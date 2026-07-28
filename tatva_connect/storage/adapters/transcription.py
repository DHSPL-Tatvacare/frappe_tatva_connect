# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""The transcription service's adapter — a producer of TEXT, on the same spine as every other producer.

THIS IS NOT A SECOND WRITE PATH. The endpoint authenticates, this validates the shape, and then it calls
`call_media.store_transcript` — the very same door the voice adapter's parser calls. If a transcript can
arrive two ways but be stored one way, there is one brain; if it could be stored two ways, there would be
two, and the day they disagreed nobody would know which text was the call.

WHAT THIS ADAPTER OWNS: knowing that a POST from this service means what it says, and refusing one that
does not. That is all. It does not own where text lives, what replaces what, or who may read it.

AUTHENTICATION IS INVENTED NOWHERE. The service is an account row, so it inherits the per-account token,
the optional HMAC, the optional IP allowlist, the raw Integration Request log and the replay button that
`webhooks.spine` already gives WhatsApp, voice and telephony.

THE POSTED SHAPE — the canonical transcript, exactly as every other producer's:
    {"call": "<CRM Call Log>", "text": "...", "segments": [{speaker?, start?, end?, text}],
     "summary": "...", "source": "whisper-v3"}
`text` or `summary` is required; everything else is optional, because sparseness is the contract — a
service returning bare prose fills in fewer boxes and needs no new code to land.
"""
import frappe

from tatva_connect.channels import contract
from tatva_connect.storage import call_media

ACCOUNT_DOCTYPE = "CRM Transcription Account"

DECLARATION = contract.declare(
	channel="transcription",
	provider="tatva",
	account_doctype=ACCOUNT_DOCTYPE,
	# A transcript is not a message and reports nothing about delivery, so this producer declares no outcome.
	outcomes=set(),
	# It carries no recording of its own; it reads ours and hands back text.
	capabilities=set(),
	# Declared because the contract requires one; nothing on this channel ever addresses a phone number.
	number_format=contract.E164_PLUS,
)


def screen(payload, event, account):
	"""(wanted, reason). A refusal is RECORDED, not dropped — the spine keeps the row and it is replayable.

	Every no here is a shape problem the service can fix and re-post. Nothing is guessed: a transcript for
	a call this CRM does not have is declined rather than filed against the nearest match, because a
	clinical note attached to the wrong patient's call is worse than one that never arrived.
	"""
	call = (payload or {}).get("call")
	if not call:
		return False, "no call was named on this transcript"
	if not frappe.db.exists(call_media.CALL_DT, call):
		return False, f"call {call} is not a call this CRM holds"
	if not ((payload or {}).get("text") or (payload or {}).get("summary")):
		return False, "the transcript carries neither text nor a summary"
	segments = (payload or {}).get("segments")
	if segments is not None and not isinstance(frappe.parse_json(segments) or [], list):
		return False, "segments must be a list"
	return True, None


def already_processed(payload, event, account):
	"""True when this exact text from this exact source is already the call's transcript.

	The cheap short-circuit, at the front of the worker. `store_transcript` compares again before it
	writes, so this is an optimisation and never the guarantee — the guarantee lives at the door.
	"""
	call = (payload or {}).get("call")
	if not call:
		return False
	current = frappe.db.get_value(
		call_media.MEDIA_DT, call, ["text", "summary", "transcript_source"], as_dict=True
	)
	return bool(current) and (current.text, current.summary, current.transcript_source) == (
		(payload or {}).get("text"), (payload or {}).get("summary"), _source(payload, account)
	)


def handle(payload, event, account):
	"""Store it. One line of intent, through the one door — a re-transcription REPLACES what was there."""
	call_media.store_transcript((payload or {}).get("call"), {
		"source": _source(payload, account),
		"summary": (payload or {}).get("summary"),
		"text": (payload or {}).get("text"),
		"segments": frappe.parse_json((payload or {}).get("segments")) or [],
		"raw": frappe.as_json(payload),
	})


def _source(payload, account):
	"""Who produced this text. The service names its own model; the account is the fallback so a transcript
	is never stored with no provenance at all."""
	return (payload or {}).get("source") or account or DECLARATION.provider


def account_for_payload(payload, event):
	"""Re-derive the receiving account on REPLAY, which carries no token.

	Answers only what is truthfully answerable: with exactly one live transcription account, the delivery
	is that account's. With none or several it declines, and the spine says the delivery cannot be replayed
	rather than attributing it to a guess.
	"""
	if not (payload or {}).get("call"):
		return None
	names = frappe.get_all(ACCOUNT_DOCTYPE, filters={"enabled": 1}, pluck="name", limit=2)
	return names[0] if len(names) == 1 else None
