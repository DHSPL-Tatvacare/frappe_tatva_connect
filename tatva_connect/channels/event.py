"""The normalized channel event — the one interface every channel adapter emits.

An adapter's only parsing job is `normalize(payload, account) -> ChannelEvent | None`. Nothing
downstream reads a provider's field name, so adding a provider adds an adapter and nothing else moves.
Modelled on `telephony.envelope`, which proved the shape on a live 179-CDR capture: a dict subclass,
so it serialises into the raw log unchanged, built only through a keyword-only `build()`.

Two rules the vocabulary exists to enforce:

  * THE VENDOR NAME NEVER LEAVES. `provider` is carried for diagnostics, but an EMITTED event is named
    `whatsapp.delivered` — never `wati.delivered`. A consumer that keys on a vendor is a consumer that
    breaks the day the vendor changes, which is the whole reason this layer exists.
  * A STATUS WITHOUT A CORRELATION ID IS NOT A STATUS. It is the provider's echo of the id we stored
    when we sent; without it there is no row to tick, and an event that cannot find its row is worse
    than no event because it looks handled. `build()` refuses one.

Frozen after construction: an event is a fact about something that already happened. Every bug where
one was edited in flight — a status quietly rewritten between the screen and the write — is a bug this
cannot have.
"""

from datetime import datetime, timezone

from frappe.utils import convert_utc_to_system_timezone, get_datetime

# Below this many digits a numeric stamp is a provider glitch, not an epoch.
_EPOCH_MIN_DIGITS = 9

# The canonical outcome vocabulary. An adapter declares the subset it can TRUTHFULLY emit. The first six
# are messaging outcomes; the last three are what a VOICE provider can report about a call it placed —
# added when the voice channel arrived, the only new vocabulary that chunk allowed. `failed` is shared.
OUTCOMES = ("sent", "delivered", "read", "replied", "clicked", "failed", "answered", "no_answer", "completed")

# What the event IS, which decides which persistence path it takes.
#   status        — the provider reporting on a message already on the wire.
#   inbound       — the customer sent us something.
#   outbound_echo — a message that left the business OUTSIDE this CRM (an agent in the vendor portal,
#                   a bot, another API client) and is being mirrored in.
KINDS = ("status", "inbound", "outbound_echo")

_FIELDS = (
	"channel", "provider", "account", "kind", "outcome",
	"correlation_id", "provider_message_id", "wamid", "subject_number", "conversation_id",
	"text", "media_url", "media_type", "filename",
	"button_id", "button_title", "reply_to",
	"error_code", "error_detail", "at", "raw",
)


class ChannelEvent(dict):
	"""One normalized channel event. A dict, so it serialises into the raw log unchanged; frozen, so
	nothing between the screen and the write can quietly rewrite what arrived."""

	__slots__ = ("_frozen",)

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		self._frozen = False

	def _freeze(self):
		self._frozen = True
		return self

	def _refuse(self, *_args, **_kwargs):
		if getattr(self, "_frozen", False):
			raise TypeError("a ChannelEvent is a fact about something that already happened — it cannot be edited")

	def __setitem__(self, key, value):
		self._refuse()
		super().__setitem__(key, value)

	def __delitem__(self, key):
		self._refuse()
		super().__delitem__(key)

	def update(self, *args, **kwargs):
		self._refuse()
		super().update(*args, **kwargs)

	def pop(self, *args, **kwargs):
		self._refuse()
		return super().pop(*args, **kwargs)

	def clear(self):
		self._refuse()
		super().clear()

	def __getattr__(self, key):
		"""Attribute read for the declared fields — `event.outcome` reads better than `event["outcome"]`
		at the dozens of call sites that only ever read one."""
		if key in _FIELDS:
			return self.get(key)
		raise AttributeError(key)


def build(
	*,
	channel,
	provider,
	account,
	kind,
	outcome=None,
	correlation_id=None,
	provider_message_id=None,
	wamid=None,
	subject_number=None,
	conversation_id=None,
	text=None,
	media_url=None,
	media_type=None,
	filename=None,
	button_id=None,
	button_title=None,
	reply_to=None,
	error_code=None,
	error_detail=None,
	at=None,
	raw=None,
) -> ChannelEvent:
	"""Assemble one event. Called by adapters; never constructed by hand.

	`correlation_id` is THE provider message id — the one the provider's own status events echo. It is
	mandatory on a status, because a status that cannot name the message it is about can tick nothing.
	"""
	if kind not in KINDS:
		raise ValueError(f"unknown channel event kind {kind!r}; known: {list(KINDS)}")
	if outcome is not None and outcome not in OUTCOMES:
		raise ValueError(f"unknown outcome {outcome!r}; known: {list(OUTCOMES)}")
	if kind == "status" and not correlation_id:
		raise ValueError("a status event must carry the correlation_id its provider echoes, or it can tick nothing")

	return ChannelEvent(
		channel=channel,
		provider=provider,
		account=account,
		kind=kind,
		outcome=outcome,
		correlation_id=correlation_id,
		# The provider's own internal id for the message. Distinct from correlation_id: a provider may
		# mint an id it never echoes on a status, and storing that one is how a message's status stops
		# updating for ever.
		provider_message_id=provider_message_id,
		wamid=wamid,
		subject_number=subject_number,
		conversation_id=conversation_id,
		text=text,
		media_url=media_url,
		media_type=media_type,
		filename=filename,
		# An interactive reply's MACHINE-READABLE identity. The title is what the human saw; the id is
		# what an automation can key on, and it is the only one that survives a copy edit.
		button_id=button_id,
		button_title=button_title,
		# The outbound message id this event answers — the quote/tap context.
		reply_to=reply_to,
		error_code=error_code,
		error_detail=error_detail,
		at=at,
		raw=raw or {},
	)._freeze()


def parse_timestamp(value):
	"""Parse a provider timestamp, or None when it cannot be read. Never guessed.

	Shared by every channel because every provider speaks the same two dialects: epoch seconds (WATI's
	`timestamp`, Acefone's `start_stamp`) and a formatted stamp (WATI's `created`, Acefone's two).
	"""
	if value in (None, "", "0"):
		return None
	text = str(value).strip()
	# Epoch seconds. Read as UTC and converted to the SITE's timezone -- `datetime.fromtimestamp`
	# would use whatever timezone the worker container happens to run in, which is not a property of
	# the event.
	if text.isdigit() and len(text) >= _EPOCH_MIN_DIGITS:
		try:
			return convert_utc_to_system_timezone(datetime.fromtimestamp(int(text), tz=timezone.utc))
		except (ValueError, OSError, OverflowError):
			return None
	try:
		return get_datetime(text)
	except Exception:
		return None


def event_name(event) -> str | None:
	"""The canonical, vendor-free name for an emitted event: `whatsapp.delivered`.

	None when the event carries no outcome — a plain inbound text is a message, not an outcome, and
	naming it anyway would invent a signal nothing measured.
	"""
	if not event or not event.get("outcome"):
		return None
	return f"{event['channel']}.{event['outcome']}"
