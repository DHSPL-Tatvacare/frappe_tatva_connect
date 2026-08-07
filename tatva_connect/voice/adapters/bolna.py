"""Bolna voice adapter — outbound AI calls, ported from the evals platform into OUR channel narrative.

WHAT IS PORTED, AND WHAT IS NOT (W7.4 pass 1). The evals adapter is async FastAPI + a SQLAlchemy
`connections` table; this is sync Frappe + a channel `DECLARATION`. Ported here: the declaration, the
classification core (`classify_outcome`/`is_terminal`/the canonical outcome + event-name map) VERBATIM in
logic, and the BATCH placement code faithfully — but CALLER-LESS, because its only caller is the W7.2
cohort drain, which does not exist yet.

PASS 2 ADDS THE HALF THAT DIALS AND THE HALF THAT WAKES: the single `place_call`, the webhook spine
surface, the read-only agent listings, and `fetch_execution` for the dormant reconciler. Nothing dials
unless an operator turns the send switch on AND enables an account — both ship off.

CORRELATION IS THE PROVIDER'S OWN ECHO. `place_call` puts OUR opaque engine token (`journey::node`, no PII)
into `user_data`; Bolna echoes it on the terminal callback; `handle` reads it back and wakes exactly that
parked journey. No lookup row, so no window in which the callback beats the row that would have matched it —
the defect WATI's message-row correlation has to live with because WATI echoes nothing.

THE NUMBER FORMAT IS DECLARED, NOT CODED. `number_format=E164_PLUS`: Bolna sits on Twilio/Plivo and needs
`+<country><national>`. There is NO formatter in this adapter — `Declaration.conform_number` is the ONE
brain, exactly as WhatsApp, and it REFUSES an uncountried number (the voice form of the same defect).
"""
import csv as _csv
import io as _io
import re
from datetime import datetime, timedelta, timezone

import frappe
import requests
from frappe.utils import now_datetime

from tatva_connect.channels import contract
from tatva_connect.storage import call_media

DECLARATION = contract.declare(
	channel="voice",
	provider="bolna",
	account_doctype="CRM AI Voice Account",
	# What Bolna can TRUTHFULLY report about a call, as bare outcome names. `placed` is NOT here — it is
	# the node's synchronous "handed to the provider" output, never a reported outcome. `failed` is
	# declared (a call can fail) and excluded from the waitable set as a synchronous output by `outcomes_of`.
	outcomes={"answered", "no_answer", "completed", "failed"},
	# Voice has none of the messaging send-side capabilities (templates/media/buttons/…); it places calls. `recording` is the one it does have, and it buys exactly `recording_ref` below — the owning, naming, retrying and privacy all live in `storage.call_media`.
	# `bypass_guardrails` says Bolna accepts a per-call "dial now, don't wait for the agent's calling window" instruction; the node offers the author a tick BECAUSE this is declared, and a provider that omits it never renders the field.
	capabilities={"recording", "bypass_guardrails"},
	number_format=contract.E164_PLUS,
)


class BolnaServiceError(RuntimeError):
	"""4xx from Bolna — non-retryable, surfaced verbatim."""


class BolnaOutcomeUnknown(RuntimeError):
	"""The dial got NO ANSWER. Not a failure — the patient may well already have been called.

	The voice twin of `whatsapp.transport.OutcomeUnknown`, and it exists for that module's reason: a
	request that timed out may have reached the provider and started ringing, so a caller that files it
	as failed lands it in a bin whose recovery action is REPLAY, and a replayed dial calls the patient a
	second time.

	DELIBERATELY NOT A SUBCLASS of `BolnaServiceError`, which catches at `sends._deliver_voice` to give the
	contact-cap slot back and re-raise. Today the clause ORDER is what keeps them apart — the unknown is
	matched first — so inheritance would be survivable there and nowhere else: any second caller that
	handles only `BolnaServiceError`, or a reordering of those two clauses, would silently un-count and
	replay a dial that may already have reached the patient. Two outcomes, two types, no ordering to rely on.
	"""


# ── classification core — PORTED VERBATIM IN LOGIC (constraint 2). A wrong outcome map routes a real
# call's result down the wrong branch, so the status tokens, the no-reach set and the safe default are
# copied exactly and locked by a table test. ────────────────────────────────────────────────────────
#
# `call-disconnected` IS NOT TERMINAL, and treating it as one was a live defect. Bolna's own status
# reference is explicit: "Only `completed` is the final status for every conversation" — `call-disconnected`
# only means the audio ended, and `completed` follows 2-3 minutes later once recording and extraction are
# done. Four live calls confirmed it, four for four, across every ending (inactivity timeout, voicemail,
# real conversation). Left in this set it fired FIRST and classified as `no_answer`, so a journey parked on
# `voice.completed` woke early carrying "nobody picked up" for a call the patient had answered and talked
# through. Screened out, the journey waits the extra two minutes and gets the truth.
#
# The rest are the statuses where NO conversation happens, so no post-call processing follows and no
# `completed` ever arrives. They must stay terminal or those journeys park for ever.
_TERMINAL_STATUSES = frozenset({
	"completed",                                  # the one true final status
	"busy", "no-answer", "balance-low",           # unanswered — the call never connected
	"canceled", "cancelled", "failed", "stopped", "error",  # unsuccessful; both spellings of canceled
})
# "Didn't reach the person" — retry-worthy, routed like RNR. `rnr` is not in Bolna's enum; it is kept
# because it can appear inside `status_reason`, which this also matches against.
_NO_REACH_TOKENS = ("no-answer", "rnr", "busy")

# Bag key the canonical voice outcome lands on — the single source the runtime write and the downstream picker both read.
OUTCOME_BAG_FIELD = "voice_outcome"

# What an AI call is called in `CRM Call Log.telephony_medium` — a MIXED table, so the medium is what
# distinguishes automation's rows from a provider's and a rep's. Appended to the Select by a code
# Property Setter, never by editing the fork's JSON.
CALL_MEDIUM = "AI Voice"

# What a canonical outcome MEANS to a call log, beside the classifier that already owns outcome meaning.
# One map: a second dialect at each call site is how "no answer" starts meaning two things.
CALL_STATUS = {
	"answered": "Completed",
	"no_answer": "No Answer",
	"failed": "Failed",
}


def classify_outcome(status, status_reason):
	s = (status or "").lower()
	r = (status_reason or "").lower()
	if (
		s in ("completed", "answered", "success")
		and "no-answer" not in r and "rnr" not in r and "busy" not in r
	):  # `answered`/`success` are not in Bolna's enum; kept as tolerant aliases, `completed` is the real one
		return "bolna_answered"
	if any(tok in s or tok in r for tok in _NO_REACH_TOKENS):
		return "bolna_rnr"
	return "bolna_failed"


def _canonical_outcome(action_type):
	if action_type == "bolna_answered":
		return "answered"
	if action_type == "bolna_rnr":
		return "no_answer"
	return "failed"


# Single source of truth: canonical voice outcome → the event name a Wait matches on.
_VOICE_EVENT_NAMES = {
	"answered": "voice.answered",
	"no_answer": "voice.no_answer",
	"failed": "voice.failed",
}
_VOICE_EVENT_COMPLETED = "voice.completed"


def voice_event_name(canonical_outcome):
	return _VOICE_EVENT_NAMES.get(canonical_outcome, "voice.failed")


def voice_resume_event_names(canonical_outcome):
	return frozenset({voice_event_name(canonical_outcome), _VOICE_EVENT_COMPLETED})


def is_terminal(status):
	return bool(status) and status.lower() in _TERMINAL_STATUSES


def _resolve_from_phone(override, connection_default):
	cleaned_override = (override or "").strip()
	if cleaned_override:
		return cleaned_override
	cleaned_default = (connection_default or "").strip()
	if cleaned_default:
		return cleaned_default
	return None


# The `user_data` key carrying OUR opaque engine correlation token (`journey::node`, no PII). Bolna echoes
# `user_data` on its terminal webhook, so this is the ONE key `place_call` writes and the webhook reads —
# correlation rides the provider's echo, not a lookup row (WATI, which cannot echo, stores a row instead).
USER_DATA_CORRELATION_KEY = "recipient_id"


def place_call(connection, to_number, agent_id, from_override, correlation, variables=None,
               bypass_guardrails=False):
	"""Place ONE outbound call now: POST /call → execution_id. The node-facing caller (our engine is
	one-journey-per-lead); the cohort/batch path is `place_call_batch`, W7.2.

	`to_number` is ALREADY conformed to +E.164 by the send path's declared `number_format` — this adapter
	adds NO formatter of its own (the pass-1 one-brain rule). `correlation` is the engine token, placed in
	`user_data` so the terminal webhook wakes THIS parked journey and no other.

	`bypass_guardrails` is the AUTHOR'S own tick, carried straight through: dial immediately instead of
	waiting for the agent's configured calling hours. It is the channel's declared word (`contract`), and
	THIS is where it becomes Bolna's — the body key below is spelt `bypass_call_guardrails`, which is a
	fact about the vendor and stops here. No switch is read: placing a call at all is already gated by
	`AI Voice::Channel::calls`, and a second switch that silently voided the tick was removed for saying
	nothing an operator could see. Default False: an adapter told nothing must never skip a calling window.

	NOTE THE SHAPE, and it differs from the batch path by Bolna's own contract: this is a JSON body, so the
	flag is a real bool; `place_call_batch` posts multipart form fields, where it is the STRING "true".
	"""
	api_key = connection.get("api_key") or ""
	base_url = (connection.get("base_url") or "https://api.bolna.ai").rstrip("/")
	if not api_key:
		raise BolnaServiceError("Bolna connection missing api_key")
	from_phone = _resolve_from_phone(from_override, connection.get("from_phone"))
	body = {
		"agent_id": agent_id,
		"recipient_phone_number": to_number,
		"user_data": {**(variables or {}), USER_DATA_CORRELATION_KEY: correlation or ""},
	}
	if from_phone:
		body["from_phone_number"] = from_phone
	# Sent only when asked for: a `false` and an absent key read the same to Bolna, the absent one to us too.
	if bypass_guardrails:
		body["bypass_call_guardrails"] = True
	headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
	try:
		resp = requests.post(f"{base_url}/call", json=body, headers=headers, timeout=30.0)
	except Exception as e:
		# No answer is not a refusal: the dial may already be ringing, so it must never be replayed.
		raise BolnaOutcomeUnknown(str(e)[:400]) from e
	if 400 <= resp.status_code < 500:
		raise BolnaServiceError(f"Bolna {resp.status_code}: {_safe_error(resp)}")
	resp.raise_for_status()
	raw = resp.json() if resp.content else {}
	execution_id = str(raw.get("execution_id") or "")
	if not execution_id:
		raise BolnaServiceError("Bolna /call response missing execution_id — cannot correlate inbound webhooks")
	# `from_phone` rides back so the call-log write knows which number was dialled from without re-reading
	# the account — it was already resolved here.
	return {"correlation_id": execution_id, "contact": to_number, "mode": "single",
	        "from_phone": from_phone, "raw": raw}


# ── inbound webhook — the spine's duck-typed surface ────────────────────────────────────────────────
# Bolna posts ONE terminal callback per execution and ECHOES `user_data`, which is where `place_call`
# put the engine token. So the wake needs no lookup row and has no commit-race window: WATI cannot echo
# and therefore stores the token on a message row, and this is the same correlation with the row removed.
# Everything below goes through `webhooks.spine` — it authenticates, kill-switches, raw-logs and ACKs
# before any line of this file runs.


def _as_dict(value):
	return value if isinstance(value, dict) else {}


def engine_token(payload):
	"""OUR opaque `journey::node` token, read back off Bolna's echo. `place_call` writes it into `user_data`;
	the batch path (W7.2) carries the same key under `context_details.recipient_data`, so both are read."""
	user_data = _as_dict(payload.get("user_data"))
	recipient_data = _as_dict(_as_dict(payload.get("context_details")).get("recipient_data"))
	token = user_data.get(USER_DATA_CORRELATION_KEY) or recipient_data.get(USER_DATA_CORRELATION_KEY)
	return str(token or "").strip()


def execution_id_of(payload):
	"""Bolna's own id for this execution — AUDIT ONLY, never the correlation key. The webhook and
	`GET /executions/{id}` key it under `id`; only the `/call` response calls it `execution_id`."""
	return str(payload.get("id") or payload.get("execution_id") or payload.get("batch_id") or "")


# A transcript is a whole conversation; it rides a signal payload into journey state, so it is bounded here.
_TRANSCRIPT_LIMIT = 10000


def _extract_capture(event):
	"""What the call captured. Ported from the evals `_extract_capture`: Bolna splits some fields between
	the top level and a `telephony_data` nest, so each is looked for in both."""
	telephony = _as_dict(event.get("telephony_data"))

	def pick(key, *fallbacks):
		value = event.get(key)
		if value is not None:
			return value
		for fallback in fallbacks:
			value = event.get(fallback)
			if value is not None:
				return value
			value = telephony.get(fallback)
			if value is not None:
				return value
		return telephony.get(key)

	transcript = pick("transcript")
	return {
		"transcript": str(transcript)[:_TRANSCRIPT_LIMIT] if transcript else None,
		"recording_url": pick("recording_url", "recordingUrl"),
		"duration_sec": pick("duration", "duration_seconds"),
		"extracted_data": pick("extracted_data"),
		"error_message": pick("error_message", "error"),
		"hangup_reason": pick("hangup_reason", "status_reason"),
		# Bolna's own one-line account of the call. Live-verified present on every terminal callback, and
		# the single most useful thing for a human reading a journey after the fact.
		"summary": pick("summary"),
		"total_cost": pick("total_cost", "cost"),
		# HOW LONG IT RANG — the only usable signal for "a person picked up" vs "the carrier rolled over to
		# voicemail". Live: 5s on a human answer, 23s on a voicemail rollover. A heuristic, and named as one.
		"ring_duration_sec": pick("ring_duration"),
		# The provider's own answering-machine flag. Documented, but it came back null on a call that was
		# demonstrably voicemail, so it is carried through as-is and never treated as authoritative.
		"answered_by_voice_mail": event.get("answered_by_voice_mail"),
	}


def normalize_webhook(payload):
	"""One Bolna callback -> (canonical outcome, capture). The classification is the pass-1 core, asked
	once — there is no second classifier here and none in the reconciler."""
	action_type = classify_outcome(payload.get("status"), payload.get("status_reason"))
	return _canonical_outcome(action_type), _extract_capture(payload)


def waitable_signals(canonical_outcome):
	"""The event names this outcome may wake a Wait on — the resume set, narrowed to what the node
	DECLARES it can emit. `voice.failed` is a synchronous output of the node, so it is excluded there and
	a failed call therefore wakes only the coarse `voice.completed`. One source, asked, never restated."""
	from tatva_connect.workflow_engine import registry

	offered = set(registry.outcomes_for("AI Voice Call"))
	return sorted(name for name in voice_resume_event_names(canonical_outcome) if name in offered)


def screen(payload, event, account):
	"""(wanted, reason). Two questions, and a no to either is recorded rather than dropped: the spine
	persists a declined delivery with this reason, so an operator can read it and replay it."""
	status = payload.get("status")
	if not is_terminal(status):
		return False, f"call status {status or '(none)'} is not terminal — the call is still in flight"
	if not engine_token(payload):
		return False, "no workflow correlation token on this call — it was not placed by a workflow"
	return True, None


def already_processed(payload, event, account):
	"""True once every signal this callback would deliver is already in the inbox. A provider re-sending
	a terminal callback must not wake the journey twice; the spine collapses byte-identical copies, this
	catches a copy that differs in some field the wake does not read."""
	correlation = engine_token(payload)
	if not correlation:
		return False
	outcome, _capture = normalize_webhook(payload)
	names = waitable_signals(outcome)
	if not names:
		return False
	return all(
		frappe.db.exists("CRM Workflow Signal", {"correlation": correlation, "event_name": name})
		for name in names
	)


def handle(payload, event, account):
	"""Wake the journey that placed THIS call — never another journey on the same lead.

	The engine token identifies the journey AND the node, so the subject is read off the journey rather than
	guessed from the number Bolna dialled: two journeys on one lead park on two different tokens, and
	correlating on the lead is exactly the defect that would merge them.
	"""
	correlation = engine_token(payload)
	subject_doctype, subject_name = run_subject(correlation)
	if not subject_name:
		# A terminal call whose journey cannot be found. Never silent: a journey may be parked waiting for exactly
		# this, and the operator's only other clue would be a journey that simply stopped.
		frappe.log_error(
			title="voice: terminal call matches no workflow journey",
			message=f"correlation={correlation} execution_id={execution_id_of(payload)} status={payload.get('status')}",
		)
		return

	# The call's row on the lead is closed by the SAME callback that wakes the journey — one delivery, both
	# effects, so the timeline can never disagree with the journey about how the call ended.
	update_call_log(payload)

	outcome, capture = normalize_webhook(payload)
	signal_payload = {"outcome": outcome, "execution_id": execution_id_of(payload), **capture}

	from tatva_connect.workflow_engine import signals

	for name in waitable_signals(outcome):
		signals.deliver_signal(
			subject_doctype, subject_name, name, correlation=correlation, payload=signal_payload,
		)


# A line that names its speaker: "assistant: Hi there". ONLY this module may know that prefix exists —
# the parsing lives beside `classify_outcome` because both are "what does this provider's output mean",
# and everything else in the app sees the canonical shape.
_SPEAKER_LINE = re.compile(r"^\s*(?P<speaker>[A-Za-z][\w .-]{0,40}?)\s*:\s*(?P<text>\S.*)$")

# This provider's speaker vocabulary, folded onto the canonical ROLE. Knowing that "assistant" means the
# machine side is exactly this module's job — it already knows the prefix is there at all — and it is the
# LAST place that knowledge is allowed to live. Downstream stores a side of the call, never a word, so a
# screen is free to draw "Agent" for one and the lead's own name for the other. A prefix this provider
# has never sent leaves the segment with no role rather than inventing one.
_SPEAKER_ROLES = {
	"assistant": call_media.ROLE_AGENT,
	"agent": call_media.ROLE_AGENT,
	"bot": call_media.ROLE_AGENT,
	"ai": call_media.ROLE_AGENT,
	"user": call_media.ROLE_CONTACT,
	"human": call_media.ROLE_CONTACT,
	"customer": call_media.ROLE_CONTACT,
	"caller": call_media.ROLE_CONTACT,
}

def parse_transcript(transcript):
	"""Bolna's flat transcript string -> the canonical `{text, segments}`. None when there is nothing.

	ONE SHAPE WITH OPTIONAL PARTS. Bolna gives a speaker and no timings, so its segments carry a `role` and
	NO `start`/`end` — absent, never zero, because a padded zero would draw a timestamp that is a lie. A
	provider that returns bare prose lands as ONE segment with no role at all, which is the bottom rung of
	the reader's ladder and needs no new code to arrive on.

	The prefix is folded to a ROLE here and the word itself is thrown away: "assistant" is this provider's
	term for the machine side, and a rep should never be shown it. What a screen calls each role is the
	screen's business — this only says which side spoke.

	`text` is ALWAYS the readable whole, so nobody has to understand segments to read the call.
	"""
	text = (transcript or "").strip()
	if not text:
		return None
	segments = []
	for line in text.splitlines():
		line = line.strip()
		if not line:
			continue
		match = _SPEAKER_LINE.match(line)
		role = _SPEAKER_ROLES.get(match.group("speaker").strip().casefold()) if match else None
		if role:
			segments.append({"role": role, "text": match.group("text").strip()})
		elif segments and "role" in segments[-1]:
			# A wrapped continuation of the line above — it belongs to whoever was speaking.
			segments[-1]["text"] = f"{segments[-1]['text']} {line}".strip()
		else:
			# A prefix this provider has never sent, or none at all: kept whole and left unattributed
			# rather than guessed at, which is the plain-text rung.
			segments.append({"text": line})
	return {"text": text, "segments": segments}


def transcript_of(payload):
	"""This callback's transcript in the CANONICAL shape, or None. The parser's output plus provenance.

	Knowing that "assistant:" names a speaker is this module's job and nobody else's; knowing where a
	transcript is stored is `storage.call_media`'s. This function is the seam between the two, and it is
	the whole of what the adapter contributes to the text half.

	`raw` keeps the provider's own transcript payload untouched — including its public recording URL, for
	audit. When a parser turns out wrong, and one will, this re-derives instead of re-transcribing.
	"""
	parsed = parse_transcript(payload.get("transcript"))
	summary = payload.get("summary")
	if not parsed and not summary:
		return None
	return {
		"source": DECLARATION.provider,
		"summary": summary,
		"text": (parsed or {}).get("text"),
		"segments": (parsed or {}).get("segments") or [],
		"raw": frappe.as_json({
			**{k: payload.get(k) for k in ("transcript", "summary", "extracted_data")},
			"recording_url": _extract_capture(payload).get("recording_url"),
		}),
	}


def recording_ref(payload):
	"""WHERE THIS CALL'S AUDIO IS — the `recording` capability's one function, and the whole of it.

	Three answers and no fourth. A terminal callback carrying a URL is "here it is"; a callback that is
	not terminal yet is "not ready" — the provider publishes the recording as part of the post-call
	processing that `completed` marks the end of; a terminal callback with no URL is settled: this call
	produced no audio.

	The URL is PUBLIC — `GET /recordings/call/<id>` answers 200 audio/mpeg with no authentication — which
	is exactly why the bytes are pulled into our own storage and this ref is never handed to a player.
	No headers, because none are needed. Nothing here fetches, owns, names or retries anything.
	"""
	url = _extract_capture(payload).get("recording_url")
	if url:
		return contract.RecordingRef(url=url, provider=DECLARATION.provider)
	if not is_terminal(payload.get("status")):
		return contract.RecordingRef(pending=True)
	return contract.RecordingRef()


def update_call_log(payload):
	"""WRITE TWO: finish the call log row this execution opened. A no-op for anything else.

	Found by `id`, which IS the row's name (`CRM Call Log` autonames from a UNIQUE `id`), so this is a
	primary-key seek — no correlation column, no new index, no scan.

	A REPEATED DELIVERY IS A CHEAP NO-OP. One live Acefone CDR arrived ELEVEN times byte for byte; a
	read-modify-write on every copy would churn the row and everything indexed off it. The status is read
	first and the write is skipped when it would change nothing.

	`call-disconnected` never reaches here as terminal — `is_terminal` says only `completed` ends a call,
	settled on vendor docs plus four live calls.
	"""
	execution_id = execution_id_of(payload)
	if not execution_id or not is_terminal(payload.get("status")):
		return
	current = frappe.db.get_value("CRM Call Log", execution_id, ["status", "recording_url"], as_dict=True)
	if not current:
		return  # a call this CRM never placed — the webhook does not invent rows

	from tatva_connect.storage import call_media

	# Both artifacts go through the shared doors on THIS callback. Before the early-out below, because a
	# redelivery that finds the call row already closed may still be the first to carry a transcript.
	call_media.store_transcript(execution_id, transcript_of(payload))

	outcome, capture = normalize_webhook(payload)
	status = CALL_STATUS.get(outcome, "Failed")
	values = {"status": status, "end_time": now_datetime()}
	if capture.get("duration_sec") is not None:
		values["duration"] = _as_seconds(capture["duration_sec"])
	# The bytes become OURS, and the row points at our file — never at the provider's public URL.
	ours = call_media.store_recording(execution_id, recording_ref(payload))
	if ours:
		values["recording_url"] = ours
	if current.status == status and current.recording_url == values.get("recording_url", current.recording_url):
		return  # already closed by an earlier copy of this same delivery
	frappe.db.set_value("CRM Call Log", execution_id, values, update_modified=False)


def _as_seconds(value):
	"""A Duration field is whole seconds; the provider reports a float."""
	try:
		return int(float(value))
	except (TypeError, ValueError):
		return None


def run_subject(correlation):
	"""(subject_doctype, subject_name) for the journey half of a `journey::node` token, or (None, None)."""
	journey = (correlation or "").split("::", 1)[0].strip()
	if not journey:
		return None, None
	row = frappe.db.get_value(
		"CRM Workflow Journey", journey, ["subject_doctype", "subject_name"], as_dict=True,
	)
	return (row.subject_doctype, row.subject_name) if row else (None, None)


def account_for_payload(payload, event):
	"""Re-derive the receiving account on REPLAY, which carries no token.

	Bolna's callback names no account of its own, and nothing about the wake needs one — the engine token
	identifies the journey without help. So this answers the only question that is truthfully answerable: when
	exactly one voice account is live, the delivery is that account's. With none or several it declines,
	and the spine says the delivery cannot be replayed rather than attributing it to a guess.
	"""
	names = frappe.get_all("CRM AI Voice Account", filters={"enabled": 1}, pluck="name", limit=2)
	return names[0] if len(names) == 1 else None


# ── read-only provider listings — the inspector's agent picker ──────────────────────────────────────
# Called SERVER-side (voice/api.py) so the api key never reaches a browser. Read-only: nothing here can
# place a call.


def _get(connection, path):
	api_key = connection.get("api_key") or ""
	base_url = (connection.get("base_url") or "https://api.bolna.ai").rstrip("/")
	if not api_key:
		raise BolnaServiceError("Bolna connection missing api_key")
	headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
	resp = requests.get(f"{base_url}{path}", headers=headers, timeout=30.0)
	if 400 <= resp.status_code < 500:
		raise BolnaServiceError(f"Bolna {resp.status_code}: {_safe_error(resp)}")
	resp.raise_for_status()
	return resp.json() if resp.content else None


def list_agents(connection):
	"""Every agent on the account, as [{id, name, status, type}]. `GET /v2/agent/all`."""
	payload = _get(connection, "/v2/agent/all")
	if isinstance(payload, dict) and "agents" in payload:
		payload = payload["agents"]
	if not isinstance(payload, list):
		raise BolnaServiceError(f"Bolna /v2/agent/all returned unexpected shape: {type(payload).__name__}")
	return [
		{
			"id": str(raw.get("id") or raw.get("agent_id") or ""),
			"name": str(raw.get("agent_name") or raw.get("name") or ""),
			"status": str(raw.get("agent_status") or raw.get("status") or ""),
			"type": str(raw.get("agent_type") or raw.get("type") or ""),
		}
		for raw in payload
		if isinstance(raw, dict)
	]


# A `{token}` in a Bolna prompt or welcome message. Bolna has no declared-variable field — an agent's
# variables ARE its placeholders, substituted at call time from `user_data`. Live-proven: `{customer_name}`
# spoke as "Hi Pareekshith" and `{customer_plan_name}` as "Your Diabetes Program plan is now active".
_TOKEN_RE = re.compile(r"\{(\w+)\}")


def agent_variables(connection, agent_id):
	"""The placeholder names THIS agent will substitute — the slots an author has to fill.

	Read from the agent itself, never typed: an unfilled placeholder is not a blank, it is the literal
	text "Hi {customer_name}" spoken down the phone to a patient.
	"""
	agent = get_agent(connection, agent_id)
	surface = f"{agent.get('prompt') or ''}\n{agent.get('welcome_message') or ''}"
	return sorted(set(_TOKEN_RE.findall(surface)))


def get_agent(connection, agent_id):
	"""One agent, normalised to {id, name, prompt, welcome_message}. `GET /v2/agent/{id}`.

	The VENDOR's shape is unpacked here and nowhere else — an author reading the prompt before choosing an
	agent must not require the inspector to know that Bolna nests system prompts under `agent_prompts`.
	Ported from the evals `extract_variables`: every task's system prompt, then the top-level one.
	"""
	if not agent_id:
		raise BolnaServiceError("get_agent requires an agent_id")
	raw = _get(connection, f"/v2/agent/{agent_id}") or {}
	prompts = []
	for task in _as_dict(raw.get("agent_prompts")).values():
		system_prompt = _as_dict(task).get("system_prompt")
		if isinstance(system_prompt, str) and system_prompt:
			prompts.append(system_prompt)
	top_level = raw.get("system_prompt")
	if isinstance(top_level, str) and top_level:
		prompts.append(top_level)
	welcome = raw.get("agent_welcome_message")
	return {
		"id": str(raw.get("id") or raw.get("agent_id") or agent_id),
		"name": str(raw.get("agent_name") or raw.get("name") or ""),
		"prompt": "\n\n".join(prompts),
		"welcome_message": welcome if isinstance(welcome, str) else "",
	}


def list_phone_numbers(connection):
	"""The caller-id numbers this account really owns, as [{id, name}]. `GET /phone-numbers/all`.

	Picked, never typed — a from-number the provider does not own is rejected at dial time, which is a
	failed journey discovered on a live lead rather than a greyed-out option at author time.
	"""
	payload = _get(connection, "/phone-numbers/all")
	if not isinstance(payload, list):
		raise BolnaServiceError(f"Bolna /phone-numbers/all returned unexpected shape: {type(payload).__name__}")
	# The NUMBER is the label — `telephony_provider` is the same word on every row and named none of them.
	# The carrier rides as `status`, which `voice.api._listing:57` already joins on: no second joiner here.
	return [
		{
			"id": str(raw.get("phone_number")),
			"name": str(raw.get("phone_number")),
			"status": str(raw.get("telephony_provider") or ""),
		}
		for raw in payload
		if isinstance(raw, dict) and raw.get("phone_number")
	]


def fetch_execution(connection, execution_id):
	"""One execution's current state — what the reconciler polls when a webhook never arrived."""
	if not execution_id:
		raise BolnaServiceError("fetch_execution requires an execution_id")
	return _get(connection, f"/executions/{execution_id}") or {}


def _build_batch_csv(requests_, recipient_ids):
	extras = sorted({k for req in requests_ for k in (req.get("variables") or {}).keys()})
	columns = ["contact_number", "recipient_id", *extras]
	buf = _io.StringIO()
	writer = _csv.DictWriter(buf, fieldnames=columns)
	writer.writeheader()
	for rid, req in zip(recipient_ids, requests_, strict=True):
		row = {"contact_number": req.get("contact"), "recipient_id": rid}
		for col in extras:
			row[col] = (req.get("variables") or {}).get(col, "")
		writer.writerow(row)
	return buf.getvalue().encode("utf-8")


# W7.2 (cohort drain) IS THE ONLY FUTURE CALLER of `place_call_batch`. It is caller-less today — the
# workflow NODE places a SINGLE call (one journey per lead) and never enters this path. Ported faithfully so
# the cohort optimisation exists when W7.2 arrives; wire it there, never to the node.
def place_call_batch(connection, requests_, recipient_ids):
	"""Place a COHORT of calls in one Bolna batch: POST /batches (a CSV of recipients) → batch_id →
	schedule it. Sync port of the evals adapter; `connection` is {api_key, base_url, from_phone}."""
	if not requests_:
		return []
	if len(requests_) != len(recipient_ids):
		raise BolnaServiceError("place_call_batch: requests and recipient_ids length mismatch")
	api_key = connection.get("api_key") or ""
	base_url = (connection.get("base_url") or "https://api.bolna.ai").rstrip("/")
	if not api_key:
		raise BolnaServiceError("Bolna connection missing api_key")

	first = requests_[0]
	agent_id = first.get("agent_id")
	from_phone = _resolve_from_phone(first.get("from_phone"), connection.get("from_phone"))

	csv_bytes = _build_batch_csv(requests_, recipient_ids)
	data = {"agent_id": agent_id}
	if from_phone:
		data["from_phone_numbers"] = from_phone
	# Our word in, Bolna's word out — the same translation `place_call` does, and the same reason.
	if first.get("bypass_guardrails"):
		data["bypass_call_guardrails"] = "true"

	headers = {"Authorization": f"Bearer {api_key}"}
	files = {"file": ("cohort.csv", _io.BytesIO(csv_bytes), "text/csv")}
	resp = requests.post(f"{base_url}/batches", data=data, files=files, headers=headers, timeout=60.0)
	if 400 <= resp.status_code < 500:
		raise BolnaServiceError(f"Bolna {resp.status_code}: {_safe_error(resp)}")
	resp.raise_for_status()
	raw = resp.json() if resp.content else {}

	batch_id = str(raw.get("batch_id") or "")
	if not batch_id:
		raise BolnaServiceError("Bolna /batches response missing batch_id — cannot correlate inbound webhooks")
	when = datetime.now(timezone.utc) + timedelta(minutes=2)
	_schedule_batch(base_url, api_key, batch_id, when, bool(first.get("bypass_guardrails")))
	return [
		{"correlation_id": batch_id, "contact": req.get("contact"), "mode": "batch", "raw": raw}
		for req in requests_
	]


def _schedule_batch(base_url, api_key, batch_id, when, bypass):
	# Bolna requires a +00:00 literal — a Z suffix causes 400.
	data = {"scheduled_at": when.strftime("%Y-%m-%dT%H:%M:%S+00:00")}
	if bypass:
		data["bypass_call_guardrails"] = "true"
	headers = {"Authorization": f"Bearer {api_key}"}
	resp = requests.post(f"{base_url}/batches/{batch_id}/schedule", data=data, headers=headers, timeout=60.0)
	if 400 <= resp.status_code < 500:
		raise BolnaServiceError(f"Bolna {resp.status_code}: {_safe_error(resp)}")
	resp.raise_for_status()


def _safe_error(resp):
	try:
		return resp.json()
	except Exception:
		return {"text": resp.text[:200]}
