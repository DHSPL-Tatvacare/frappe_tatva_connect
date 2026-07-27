"""Bolna voice adapter — outbound AI calls, ported from the evals platform into OUR channel narrative.

WHAT IS PORTED, AND WHAT IS NOT (W7.4 pass 1). The evals adapter is async FastAPI + a SQLAlchemy
`connections` table; this is sync Frappe + a channel `DECLARATION`. Ported here: the declaration, the
classification core (`classify_outcome`/`is_terminal`/the canonical outcome + event-name map) VERBATIM in
logic, and the BATCH placement code faithfully — but CALLER-LESS, because its only caller is the W7.2
cohort drain, which does not exist yet.

DELIBERATELY ABSENT until pass 2: the SINGLE `place_call` (the node-facing HTTP call site), the
`CRM Bolna Account` doctype, the webhook ingress, the reconciler, the agent-list fetch. So nothing here
can dial a real person: the send switch ships OFF, no account rows exist, and the declaration resolves its
account lazily.

THE NUMBER FORMAT IS DECLARED, NOT CODED. `number_format=E164_PLUS`: Bolna sits on Twilio/Plivo and needs
`+<country><national>`. There is NO formatter in this adapter — `Declaration.conform_number` is the ONE
brain, exactly as WhatsApp, and it REFUSES an uncountried number (the voice form of the Turkey disaster).
"""
import csv as _csv
import io as _io
from datetime import datetime, timedelta, timezone

import requests

from tatva_connect.channels import contract

DECLARATION = contract.declare(
	channel="voice",
	provider="bolna",
	account_doctype="CRM Bolna Account",
	# What Bolna can TRUTHFULLY report about a call, as bare outcome names. `placed` is NOT here — it is
	# the node's synchronous "handed to the provider" output, never a reported outcome. `failed` is
	# declared (a call can fail) and excluded from the waitable set as a synchronous output by `outcomes_of`.
	outcomes={"answered", "no_answer", "completed", "failed"},
	# Voice has none of the messaging send-side capabilities (templates/media/buttons/…); it places calls.
	capabilities=set(),
	number_format=contract.E164_PLUS,
)


class BolnaServiceError(RuntimeError):
	"""4xx from Bolna — non-retryable, surfaced verbatim."""


# ── classification core — PORTED VERBATIM IN LOGIC (constraint 2). A wrong outcome map routes a real
# call's result down the wrong branch, so the status tokens, the no-reach set and the safe default are
# copied exactly and locked by a table test. ────────────────────────────────────────────────────────
_TERMINAL_STATUSES = frozenset({
	"completed", "answered", "success",
	"failed", "canceled", "cancelled",
	"no-answer", "rnr", "busy",
	"error", "stopped", "balance-low",
	"call-disconnected",
})
# "Didn't reach the person" — retry-worthy, routed like RNR; includes call-disconnected (connected then dropped).
_NO_REACH_TOKENS = ("no-answer", "rnr", "busy", "call-disconnected")

# Bag key the canonical voice outcome lands on — the single source the runtime write and the downstream picker both read.
OUTCOME_BAG_FIELD = "voice_outcome"


def classify_outcome(status, status_reason):
	s = (status or "").lower()
	r = (status_reason or "").lower()
	if (
		s in ("completed", "answered", "success")
		and "no-answer" not in r and "rnr" not in r and "busy" not in r
	):
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
# workflow NODE places a SINGLE call (one run per lead) and never enters this path. Ported faithfully so
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
	if first.get("bypass_call_guardrails"):
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
	_schedule_batch(base_url, api_key, batch_id, when, bool(first.get("bypass_call_guardrails")))
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
