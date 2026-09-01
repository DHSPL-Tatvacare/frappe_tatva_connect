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

# The two timestamp dialects are not telephony's — a WhatsApp history item speaks the same pair. One
# parser in the channel layer, re-exported here so `env.parse_timestamp` keeps naming it.
from tatva_connect.channels.event import parse_timestamp

# Below this a value is a provider glitch or an internal extension, not a subscriber number.
# Suffix-matching on it would match a large slice of the lead table, so it is rejected instead.
PHONE_MIN_DIGITS = 10


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
	correlation_keys=(),
	agent_key=None,
	agent_extension=None,
	agent_name=None,
	started_at=None,
	ended_at=None,
	duration_sec=0,
	recording_ref=None,
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
		# Every id that could name the row the bridge minted for this outbound call, best first — a provider may echo the id we sent, its own, or both.
		correlation_keys=tuple(correlation_keys or ()),
		agent_key=agent_key,
		# The provider's own seat id for the agent, when it names them by seat rather than by email.
		agent_extension=agent_extension,
		agent_name=agent_name,
		started_at=started_at,
		ended_at=ended_at,
		duration_sec=duration_sec,
		# Where this call's audio is, as the adapter's whole answer (`contract.RecordingRef`), never a URL string.
		recording_ref=recording_ref,
		raw=raw or {},
	)


def to_int(value) -> int:
	"""Seconds as an int. A 0s call is real — an instant hangup — so 0 is a value, not a sentinel."""
	try:
		return int(float(value))
	except (TypeError, ValueError):
		return 0
