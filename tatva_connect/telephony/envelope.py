"""The telephony envelope — the ONE interface every provider's adapter normalizes into.

An adapter's only job is `normalize(payload, event, account) -> Envelope | None`. Nothing
downstream (the writer, grain resolution, agent resolution, the capture policy) ever sees a
provider's field name again. Adding a provider is one adapter; nothing else moves.

Why this exists, concretely: Acefone is not internally consistent. A 179-CDR live capture
(2026-07-11, `docs/plans/2026-07-11-telephony-multi-provider-intake.md`) showed ONE provider
speaking two dialects on the same webhook — different timestamp formats, different phone
formats, disjoint hangup vocabularies, different fields populated:

    direction "inbound"          -> '2026-07-11 20:39:28', bare digits,  billsec present
    direction "Dialer (inbound)" -> '7/11/2026, 9:12:56 PM', +91 digits, billsec EMPTY

So normalization is not a multi-provider luxury; a single provider already needs it. Absorbing
that variance is the adapter's job — the envelope below is always the same shape.

DIRECTION IS CARRIED, NOT INFERRED. The old code derived direction from *which URL was
registered* (four endpoints, one per Acefone trigger), so a webhook pointed at the wrong URL
silently inverted `from`/`to` with no error. Providers put direction in the body; read it
there and treat the URL as a cross-check only.
"""
import re
from datetime import datetime

# Timestamp formats seen in the wild. Acefone emits BOTH — the first on IVR-routed calls, the
# second on Dialer-routed ones, same webhook, same account.
_TS_FORMATS = (
	"%Y-%m-%d %H:%M:%S",        # 2026-07-11 20:39:28
	"%m/%d/%Y, %I:%M:%S %p",    # 7/11/2026, 9:12:56 PM
	"%Y-%m-%dT%H:%M:%SZ",       # ISO 8601 (offered by the dashboard; not currently selected)
)

# A phone we will act on must carry a full subscriber number. Shorter is a provider glitch or
# an internal extension, and suffix-matching on it would match hundreds of leads — see
# `resolve` / the attribution rule. Fail closed instead.
PHONE_MIN_DIGITS = 10


class Envelope(dict):
	"""One normalized call. A dict so it stays trivially serialisable into the raw log."""

	@property
	def is_inbound(self) -> bool:
		return self.get("direction") == "inbound"


def build(
	*,
	provider,
	account,
	call_key,
	direction,
	channel,
	customer_number,
	did_number,
	status,
	connected=False,
	correlation_key=None,
	agent_key=None,
	agent_name=None,
	started_at=None,
	ended_at=None,
	duration_sec=0,
	recording_url=None,
	raw=None,
) -> Envelope:
	"""Assemble an envelope. Adapters call this; nobody else constructs one by hand."""
	return Envelope(
		provider=provider,
		account=account,
		call_key=call_key,
		direction=direction,
		channel=channel,
		customer_number=customer_number,
		did_number=did_number,
		status=status,
		connected=bool(connected),
		# The id WE gave the provider when we placed an outbound call, echoed back — the only
		# way to tie a CDR to the row we pre-created. Acefone echoes nothing (empty on all 179
		# captured CDRs); Ozonetel echoes `uui`. None means fall back to number+recency.
		correlation_key=correlation_key,
		agent_key=agent_key,
		agent_name=agent_name,
		started_at=started_at,
		ended_at=ended_at,
		duration_sec=duration_sec,
		recording_url=recording_url,
		raw=raw or {},
	)


def phone_digits(value) -> str:
	"""A phone reduced to its last-10 subscriber digits, or '' if it isn't one.

	One provider sends the SAME number both ways — '9911232686' on an IVR call and
	'+919911232686' on a Dialer call. Normalising at the envelope boundary means nothing
	downstream ever sees two spellings of one number.

	Returns '' (never a short suffix) below PHONE_MIN_DIGITS: `LIKE '%<suffix>'` on a 2-digit
	suffix matches half the lead table.
	"""
	d = re.sub(r"\D", "", str(value or ""))
	return d[-10:] if len(d) >= PHONE_MIN_DIGITS else ""


def parse_timestamp(value):
	"""Parse a provider timestamp into a naive datetime, or None.

	Tries each known provider format, then epoch seconds. Returns None rather than guessing —
	an unparseable stamp leaves the field empty instead of inventing a time.
	"""
	if value in (None, "", "0"):
		return None
	s = str(value).strip()
	if s.isdigit() and len(s) >= 9:
		try:
			return datetime.fromtimestamp(int(s))
		except (ValueError, OSError, OverflowError):
			return None
	for fmt in _TS_FORMATS:
		try:
			return datetime.strptime(s, fmt)
		except ValueError:
			continue
	return None


def to_int(value) -> int:
	"""Seconds as an int; 0 when the provider sends blank/garbage (a 0s call is real — an
	instant hangup — so 0 is a legitimate value, not a sentinel for 'missing')."""
	try:
		return int(float(value))
	except (TypeError, ValueError):
		return 0
