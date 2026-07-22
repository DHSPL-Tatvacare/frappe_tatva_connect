"""The normalized call — the one interface every provider adapter emits.

An adapter's only job is `normalize(payload, event, account) -> Envelope | None`. Nothing downstream
reads a provider's field name. Adding a provider adds an adapter, and nothing else moves.

Normalization is not a multi-provider luxury. A 179-CDR live capture showed Acefone speaking two
dialects on one webhook: IVR-routed calls stamp `2026-07-11 20:39:28` with bare digits, Dialer-routed
calls stamp `7/11/2026, 9:12:56 PM` with a +91 prefix. Absorbing that variance is the adapter's work.

Direction is carried, never inferred from the registered URL. That inference silently inverted
`from`/`to` whenever a webhook was pointed at the wrong endpoint.
"""
import re
from datetime import datetime, timezone

from frappe.utils import convert_utc_to_system_timezone, get_datetime

# Below this a value is a provider glitch or an internal extension, not a subscriber number.
# Suffix-matching on it would match a large slice of the lead table, so it is rejected instead.
PHONE_MIN_DIGITS = 10

_EPOCH_MIN_DIGITS = 9


class Envelope(dict):
	"""One normalized call. A dict, so it serialises into the raw log unchanged."""


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
	"""Assemble an envelope. Called by adapters; never constructed by hand."""
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
		# The id handed to a provider on an outbound call and echoed back on its CDR. Acefone echoes
		# nothing (empty on all 179 captured CDRs); Ozonetel echoes `uui`.
		correlation_key=correlation_key,
		agent_key=agent_key,
		agent_name=agent_name,
		started_at=started_at,
		ended_at=ended_at,
		duration_sec=duration_sec,
		recording_url=recording_url,
		raw=raw or {},
	)


def parse_timestamp(value):
	"""Parse a provider timestamp, or None when it cannot be read. Never guessed."""
	if value in (None, "", "0"):
		return None
	text = str(value).strip()
	# Epoch seconds, which the Acefone dashboard offers as an alternative to a formatted stamp. Read as
	# UTC and converted to the SITE's timezone -- `datetime.fromtimestamp` would use whatever timezone
	# the worker container happens to run in, which is not a property of the call.
	if text.isdigit() and len(text) >= _EPOCH_MIN_DIGITS:
		try:
			return convert_utc_to_system_timezone(datetime.fromtimestamp(int(text), tz=timezone.utc))
		except (ValueError, OSError, OverflowError):
			return None
	try:
		return get_datetime(text)
	except Exception:
		return None


def to_int(value) -> int:
	"""Seconds as an int. A 0s call is real — an instant hangup — so 0 is a value, not a sentinel."""
	try:
		return int(float(value))
	except (TypeError, ValueError):
		return 0
