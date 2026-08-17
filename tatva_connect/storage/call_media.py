# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""THE CALL-MEDIA LAYER — one door in per artifact, one door out, and none of them knows a vendor's name.

Recordings and transcripts arrive from many places and always will: a voice provider hands us both in
its own webhook, a telephony provider publishes a recording minutes AFTER hangup, and a transcription
service posts text in over an API long after the call ended. THE MIDDLE MAY BE AS MESSY AS REALITY
REQUIRES. What may not be messy is the number of ways an artifact is owned:

    store_recording(call, ref)      the ONE way audio is ever owned
    store_transcript(call, text)    the ONE way text is ever stored
    media_for(call)                 the ONE question any screen asks

An adapter answers exactly ONE question — `recording_ref(payload) -> contract.RecordingRef`: here it is,
not ready yet, or there is none. Everything after that answer — dedupe, timeout, size cap, retry with
backoff, the file write, the ownership bond, privacy, naming — is written here, once, for every producer.
A vendor's name reaches this module only as DATA, riding in on the ref, and is stamped into a file name.

WHY IT LIVES BESIDE THE FILE LAYER. This module owns bytes and their ownership, so it sits with the code
that does — `file_manager`, `file_events`, `file_override`. It is deliberately not in a channel package:
a channel package is one vendor's house, and the second producer would have had to reach into the first's.

THE THREE MECHANISMS, unchanged and not re-implemented here:
  M1 OWNERSHIP — the File is born attached to `CRM Call Log / <call>`, so the blob's life is the call's
     life and `File.on_trash` drops it on the last reference. No folder scheme; folders stranded files twice.
  M2 BYTE ACCESS — nothing here reads a disk. `file_manager.save` writes and `FileOverride` serves.
  M3 DISPLAY / PRIVACY — `is_private` is set NOWHERE in this module. `file_events.may_be_public` is the
     ONE checkpoint, `CRM Call Log` is not on the operator's public allowlist, so the row lands private on
     the floor. A caller that argued with that would be the second decider.

PLAYBACK IS STORED-OR-NOTHING. `media_for` returns a URL only for bytes we hold. The producer's own URL is
kept on the media row so a failed fetch can be retried, and is never served — patient audio does not play
off somebody else's host, however unguessable the link.
"""
import mimetypes

import frappe
from frappe.utils import add_to_date, now_datetime

from tatva_connect.automation import settings
from tatva_connect.channels import retry, transfer
from tatva_connect.storage import blob_store, file_manager
from tatva_connect.workflow_engine import thresholds

MEDIA_DT = "CRM Call Media"
CALL_DT = "CRM Call Log"

# WHO IS SPEAKING, as a ROLE and never as a label. A producer's own vocabulary — "assistant", "user",
# "bot", "caller" — stops at its adapter, which is the one place allowed to know it; what is STORED is
# which SIDE of the call spoke. A label would be a decision about wording baked into a database row, and
# it would be wrong the moment a screen wants the lead's own name there instead of a generic noun. The
# role also survives the day a human agent takes one of these calls, with nothing renamed.
ROLE_AGENT = "agent"
ROLE_CONTACT = "contact"
ROLES = (ROLE_AGENT, ROLE_CONTACT)

# The recording lifecycle. Blank means no producer has spoken about audio for this call at all.
AWAITING = retry.AWAITING
STORED = retry.STORED
ABANDONED = retry.ABANDONED
# This ledger's OWN state, and the reason it is not shared: a call can be settled as having produced no audio, while a message that carried no media simply has no media state.
ABSENT = "Absent"

# Nothing more will happen to a row in one of these — the reaper counts its retention from here.
TERMINAL_RECORDING_STATES = (STORED, ABSENT, ABANDONED)

# The sweep's own switch — dormant by default, like every automation in this app.
RETRY_SWITCH = "Storage::Recording::retry"

# The sweep's one question, and the only read here that is not a primary-key seek.
RECORDING_INDEX = "ix_call_media_recording_due"
RECORDING_INDEX_FIELDS = ["recording_state", "recording_next_attempt_at"]

FETCH_TIMEOUT = 60
_CHUNK = 64 * 1024

# Backoff between fetch attempts, in minutes. Its length IS the retry budget: spend it and the row is Abandoned.
BACKOFF_MINUTES = retry.LADDER
MAX_ATTEMPTS = retry.BUDGET

# One sweep's bound. A backlog drains over several passes rather than in one long job.
SWEEP_BATCH = 50

# Content type -> extension, because the URL cannot be trusted to carry one. `mimetypes` answers most of
# these already; the map exists for the audio types where its answer is unstable across platforms.
_EXTENSIONS = {
	"audio/mpeg": ".mp3",
	"audio/mp3": ".mp3",
	"audio/wav": ".wav",
	"audio/x-wav": ".wav",
	"audio/wave": ".wav",
	"audio/ogg": ".ogg",
	"audio/webm": ".webm",
	"audio/mp4": ".m4a",
	"audio/x-m4a": ".m4a",
}
_FALLBACK_EXTENSION = ".bin"


def recording_file_name(provider, call, content_type, when=None):
	"""`<provider>_<call>_<timestamp>.<ext>` — who produced this audio, readable at a glance.

	The name is the only place a stored recording says where it came from, in the Azure container and on
	the lead's Files tab alike. `provider` is data handed in by the adapter's ref, so this formats a string
	and never learns a vendor exists.

	THE EXTENSION COMES FROM THE CONTENT TYPE, never from the URL. One provider serves `audio/mpeg` behind
	a path that ends in nothing useful and the next will serve wav behind the same shape; trusting the URL
	means a `.mp3` that is not one, which every player and every browser preview then gets wrong.

	Composed HERE and handed to the file layer as an ordinary file name. `BlobStore.new_key` is untouched
	on purpose — it builds the key for every file in the app, and restyling it would silently rename the
	shape of every future upload across the product.
	"""
	stamp = frappe.utils.get_datetime(when or now_datetime()).strftime("%Y%m%d%H%M%S")
	# `_slug` is the app's ONE rule for making a record name safe in a path segment; a second would drift.
	stem = f"{frappe.scrub(provider or 'unknown')}_{blob_store._slug(call)}_{stamp}"
	return f"{stem}{_extension_for(content_type)}"


def _extension_for(content_type):
	kind = (content_type or "").split(";")[0].strip().lower()
	return _EXTENSIONS.get(kind) or mimetypes.guess_extension(kind) or _FALLBACK_EXTENSION


def store_recording(call, ref):
	"""THE ONE WAY AUDIO IS EVER OWNED. Returns our file's URL, or None.

	`ref` is a `contract.RecordingRef` — the adapter's whole answer. Its three states are kept apart here
	because collapsing them is how a recording silently never arrives: `pending` parks the row Awaiting for
	the producer's next word, `absent` settles it, and a URL is fetched.

	A FAILED FETCH IS A GAP, NOT A FAILED JOB. The call really happened and its transcript really arrived;
	raising here would fail the webhook worker and, on a retry, re-dial a patient over a byte fetch. The
	failure lands on the media row with its reason and its next attempt, which is what the sweep reads.

	Idempotent on the ATTEMPT, not on the outcome: a redelivery finds the stored file and fetches nothing.
	"""
	if not call or not frappe.db.exists(CALL_DT, call):
		return None  # a call this CRM never placed — the media layer does not invent rows
	if ref is None:
		return None

	row = frappe.db.get_value(
		MEDIA_DT, call, ["recording_file", "recording_state", "recording_next_attempt_at"], as_dict=True
	)
	if row and row.recording_state == STORED and row.recording_file:
		# The File is asked for, not assumed. `ignore_links_on_delete` lets a call be deleted without its
		# media row blocking the cascade, so a row can outlive the File it points at — and a state of
		# "Stored" with nothing behind it would be a lie the sweep could never correct. Gone means not
		# stored, and the next delivery fetches it again.
		stored = frappe.db.get_value("File", row.recording_file, "file_url")
		if stored:
			return stored
	if row and row.recording_state == ABANDONED:
		return None  # the budget is spent; a redelivery does not buy another attempt
	if row and row.recording_next_attempt_at and row.recording_next_attempt_at > now_datetime():
		return None  # inside the backoff — a producer re-sending eleven copies must not spend eleven attempts

	if ref.pending:
		_set(call, {"recording_state": AWAITING, "recording_source": ref.provider})
		return None
	if ref.absent:
		_set(call, {"recording_state": ABSENT, "recording_source": ref.provider})
		return None

	try:
		content, content_type = _fetch(ref)
	except Exception as e:
		_record_failure(call, ref, e)
		return None

	doc = file_manager.save(
		content,
		filename=recording_file_name(ref.provider, call, content_type),
		attached_to_doctype=CALL_DT,
		attached_to_name=call,
	)
	_set(call, {
		"recording_state": STORED,
		"recording_file": doc.name,
		"recording_source": ref.provider,
		"recording_ref_url": ref.url,
		"recording_next_attempt_at": None,
		"recording_error": None,
	})
	return doc.file_url


def _fetch(ref):
	"""The bytes and what the producer says they are. Streamed, capped and timed out in ONE place.

	THE REDIRECT IS THE DELIVERY. A recording URL is a signed, short-lived hop to object storage — Bolna
	to S3, and the telephony providers the same — so refusing it reads the empty body of a 302 and reports
	"no bytes" on every attempt for ever. `transfer.fetch_capped` follows the chain and vets EVERY hop,
	which is what the SSRF guard could always have supported; it also drops our bearer token the moment
	the host changes, so a provider's credential never reaches whoever its redirect names.
	"""
	content, content_type = transfer.fetch_capped(
		ref.url,
		timeout=FETCH_TIMEOUT,
		headers=ref.headers or None,
		chunk=_CHUNK,
	)
	if not content:
		raise ValueError("the producer answered with no bytes")
	return content, content_type


def _record_failure(call, ref, error):
	"""Spend one attempt. The URL is kept so the sweep has something to retry, and never so it can be served."""
	attempts = (frappe.db.get_value(MEDIA_DT, call, "recording_attempts") or 0) + 1
	state, next_at = retry.spend(attempts)
	_set(call, {
		"recording_state": state,
		"recording_source": ref.provider,
		"recording_ref_url": ref.url,
		"recording_attempts": attempts,
		"recording_next_attempt_at": next_at,
		"recording_error": str(error)[:500],
	})


def store_transcript(call, transcript):
	"""THE ONE WAY TEXT IS EVER STORED. Returns the media row's name, or None when nothing was transcribed.

	`transcript` is the canonical shape, whoever produced it:
	    {source, text, summary, raw, segments: [{role?, start?, end?, text}]}
	`role` is `ROLE_AGENT` or `ROLE_CONTACT` — a side of the call, folded by the producer's own adapter.
	Sparseness is the point — a producer that returns bare prose fills in fewer boxes and lands with no new
	code, which is the rung a future transcription service arrives on.

	A SECOND TRANSCRIPT REPLACES THE FIRST, keeping the row's raw and source in step with it: a
	re-transcription is a correction, not history, and two texts for one call would only ever be a question
	about which is true. A REDELIVERY of the same text is a cheap no-op — the compare is done before the
	write, so eleven copies of one webhook cost eleven reads and no churn on an indexed row.
	"""
	if not call or not frappe.db.exists(CALL_DT, call):
		return None
	text = (transcript or {}).get("text")
	summary = (transcript or {}).get("summary")
	if not text and not summary:
		return None  # nothing was transcribed; an empty row would say the opposite

	values = {
		"transcript_source": (transcript or {}).get("source"),
		"summary": summary,
		"text": text,
		"segments": frappe.as_json((transcript or {}).get("segments") or []),
		"raw": (transcript or {}).get("raw"),
	}
	current = frappe.db.get_value(MEDIA_DT, call, ["text", "summary", "transcript_source"], as_dict=True)
	if current and (current.text, current.summary, current.transcript_source) == (
		text, summary, values["transcript_source"]
	):
		return call  # the same producer said the same thing again
	_set(call, values)
	return call


@frappe.whitelist()
def media_for(call):
	"""THE ONE QUESTION ANY SCREEN ASKS: what do we hold for this call?

	Visibility is decided by the PARENT — a call's artifacts are visible exactly when the call is, which is
	the same rule every sub-entity in this app follows. There is no second permission story here.

	`recording.url` is present only when the bytes are OURS. The producer's URL is on the row and is not in
	this answer: playback is stored-or-nothing, so a screen with no URL says so rather than reaching out.
	"""
	frappe.has_permission(CALL_DT, "read", call, throw=True)
	row = frappe.db.get_value(
		MEDIA_DT, call,
		["recording_state", "recording_file", "recording_source", "transcript_source", "summary", "text",
		 "segments"],
		as_dict=True,
	)
	if not row:
		return {"call": call, "recording": None, "transcript": None}

	recording = {"state": row.recording_state or None, "source": row.recording_source, "url": None,
	             "file_name": None}
	if row.recording_state == STORED and row.recording_file:
		stored = frappe.db.get_value("File", row.recording_file, ["file_url", "file_name"], as_dict=True)
		if stored:
			recording.update(url=stored.file_url, file_name=stored.file_name)
		else:
			# The row outlived its File. Say so plainly rather than claiming audio a screen cannot play.
			recording["state"] = None

	transcript = None
	if row.text or row.summary:
		transcript = {
			"source": row.transcript_source,
			"summary": row.summary,
			"text": row.text,
			"segments": frappe.parse_json(row.segments) or [],
		}
	return {"call": call, "recording": recording, "transcript": transcript}


def _set(call, values):
	"""Write the media row for this call, creating it on first use. One row per call, named after it."""
	if frappe.db.exists(MEDIA_DT, call):
		frappe.db.set_value(MEDIA_DT, call, values)
		return
	try:
		frappe.get_doc({"doctype": MEDIA_DT, "call": call, **values}).insert(ignore_permissions=True)  # authz-ok: tier-b — producer-authenticated: the webhook spine verifies a per-account token before any of this runs
	except frappe.DuplicateEntryError:
		frappe.db.set_value(MEDIA_DT, call, values)  # two copies of one delivery raced; the row exists


# ---------------------------------------------------------------------------
# The sweep — the one genuinely new piece, because media has a time dimension.
# ---------------------------------------------------------------------------
def sweep():
	"""The scheduler entry, DORMANT by default: book the retry on the `long` lane and return.

	A cron entry is enqueued by frappe on `default` with no timeout, which is 300s — a budget of six
	seconds per row for a batch that fetches from a provider and uploads to Azure. The `long` lane declares
	1500s, so the work is booked there and the tick itself stays instant. `deduplicate` is what stops a slow
	pass being stacked by the next tick; the switch is read here as well as in the job, so a dormant sweep
	queues nothing at all.
	"""
	if not settings.is_enabled(RETRY_SWITCH):
		return 0
	return frappe.enqueue(
		_sweep, queue="long", job_id="call-media-sweep", deduplicate=True,
		now=bool(frappe.flags.get("in_test")),
	)


def _sweep():
	"""Retry the recordings we are still owed. The queued half of `sweep`.

	WHAT IT RETRIES, AND WHAT IT DOES NOT. An Awaiting row carries the producer's URL exactly when the
	producer has already published one and OUR fetch failed — a timeout, a 5xx, a bad hour at their end.
	Those are what this retries, with backoff, until the budget is spent. A row parked Awaiting by a
	`pending` ref carries NO url and NO next attempt, so this never sees it: "not ready yet" is answered by
	the producer's next delivery through the webhook spine, not by us polling a URL that does not exist yet.

	THE ADAPTER-RESOLUTION RULE, written down here so the next person does not add a column for it. If a
	future producer ever has to be RE-ASKED rather than re-fetched, resolve its adapter the way the row's
	own channel already does — a telephony call through its account link, an AI voice call through the
	step-log lookup `voice.reconcile._placement_of` already performs. Do NOT add an account column to
	`CRM Call Log` for it: no producer has that problem, and the table is upstream and mixed.
	"""
	if not settings.is_enabled(RETRY_SWITCH):
		return 0
	from tatva_connect.channels import contract

	_reap()  # nothing accumulates for ever: close what can never resolve, drop what is past keeping
	retried = 0
	for row in _due_rows():
		ref = contract.RecordingRef(url=row.recording_ref_url, provider=row.recording_source)
		if store_recording(row.name, ref):
			retried += 1
		frappe.db.commit()  # per row, so a worker killed mid-sweep never re-fetches what it already stored
	return retried


def drop_for_call(doc, method=None):
	"""`CRM Call Log.on_trash` — the media row dies with the call it is about.

	It is a POINTER to a call's artifacts, not a record in its own right: with the call gone it names
	nothing, can never be reached by any screen, and would sit in the table for ever. `ignore_links_on_delete`
	already lets the call be deleted so `File.on_trash` can reclaim the blob (M1) — this is the other half
	of that decision, and without it every deleted call left an orphan.

	The File and its bytes are NOT touched here. The File is owned by the call (M1), so frappe's own
	attachment cleanup takes it on the same delete and `File.on_trash` drops the blob on the last
	reference. Deleting it from here would be a second owner for the same bytes.
	"""
	frappe.db.delete(MEDIA_DT, {"call": doc.name})


def _reap():
	"""Close what can never resolve, then delete what is past keeping. The SAME sweep, not a second one.

	Two ages, both DECLARED in `workflow_engine.thresholds` and neither restated here. An `Awaiting` row
	older than the dead-age is `Abandoned` — terminal, inert (`_due_rows` asks for `Awaiting` and nothing
	else), and still readable, which is the whole point of closing rather than deleting. A terminal row
	past the retention age is then dropped, because by then nobody is asking why the audio never arrived.
	"""
	now = now_datetime()
	frappe.db.set_value(  # authz-ok: tier-a — scheduled sweep, no user context
		MEDIA_DT,
		{"recording_state": AWAITING, "creation": ["<", add_to_date(now, days=-thresholds.MEDIA_DEAD_AFTER_DAYS)]},
		{"recording_state": ABANDONED, "recording_error": "No producer ever resolved this recording"},
	)
	frappe.db.delete(MEDIA_DT, {
		"recording_state": ["in", TERMINAL_RECORDING_STATES],
		"modified": ["<", add_to_date(now, days=-thresholds.MEDIA_RETENTION_DAYS)],
	})
	frappe.db.commit()


def _due_rows():
	"""The Awaiting rows whose next attempt has come. Equality then range — one index answers the whole
	question, and a row with no next attempt is not due by definition, so SQL's own NULL semantics keep
	the `pending` rows out without a second flag."""
	return frappe.get_all(  # authz-ok: tier-a — scheduler context, artifact state on system rows
		MEDIA_DT,
		filters={
			"recording_state": AWAITING,
			"recording_next_attempt_at": ["<=", now_datetime()],
			"recording_ref_url": ["is", "set"],
		},
		fields=["name", "recording_ref_url", "recording_source"],
		order_by="recording_next_attempt_at asc",
		limit=SWEEP_BATCH,
	)
