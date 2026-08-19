"""What a channel adapter declares, and what a send returns.

An adapter is a duck-typed module (no ABC — the same choice the webhook spine already made). It
DECLARES a `DECLARATION` and it IMPLEMENTS a fixed surface:

  send_template(account, to, template, variables) -> SendResult
  list_templates(account)                         -> [{name, ...}]
  template_variables(account, template)           -> [str, ...]  (ordered)
  normalize(payload, account)                     -> ChannelEvent | None
  screen(payload, event, account)                 -> (wanted, reason)          [spine contract]
  already_processed(payload, event, account)      -> bool                      [spine contract]
  account_for_payload(payload, event)             -> account | None            [spine contract]
  handle(payload, event, account)                 -> None                      [spine contract]

Plus one method per READ capability it declares, and none if it declares none:

  history(account, target)                        -> [item, ...]               [backfill]
  normalize_history(item, account, number)        -> ChannelEvent | None       [backfill]
  recover_message(account, conversation, msg_id)  -> item | None               [recover_message]
  fetch_media_by_message_id(account, msg_id)      -> (bytes, filename) | None  [recover_media]
  recording_ref(payload)                          -> RecordingRef              [recording]

`normalize` and `normalize_history` are TWO PRODUCERS OF ONE ENVELOPE — the webhook shape and the
provider's history shape both come out as a `ChannelEvent`, and nothing downstream can tell which was
which. That is the whole boundary: a vendor's field names stop here.

The declaration is the single source of truth, read by the ingress, the send path and the chat UI.
`outcomes` is deliberately a subset the adapter can TRUTHFULLY emit: a provider that cannot tell a
read from a delivery must not be allowed to claim `read`, because a consumer downstream will believe
it. `capabilities` is the same promise for the send side — the UI hides a template picker for a
provider that has no templates rather than offering a button that will throw.

`number_format` is the same logic applied to the address. It is a statement of what THIS provider
really requires, not of what we wish were true — see `NUMBER_FORMATS` for why it cannot be one global
rule, and what it cost the day it was one.
"""
import re
from dataclasses import dataclass

from tatva_connect.channels.event import OUTCOMES

# What a provider can DO, as opposed to what it can REPORT (that is `outcomes`). The first six are send-side. The next two are READ-side, and they are what makes an orphan status recoverable: `recover_message` = "I can hand back the ONE message a status names", `recover_media` = "and that message's file, by its id". A provider that declares neither is not broken — its orphan statuses are logged and dropped, which is what happened to every provider before either existed.
# `recording` is the media-side member of the same vocabulary: "a call I carried can produce audio, and I can say where it is". It buys ONE adapter function, `recording_ref(payload) -> RecordingRef`, and nothing else — the owning, the naming, the retrying and the privacy all live once in `storage.call_media`, which never learns a vendor's name.
# `bypass_guardrails` is "I accept a per-call instruction to dial NOW rather than wait for the agent's configured calling window". It is a request field some providers offer and others do not, so it is declared like any other capability — and the ONE author-facing word for it is this one; a vendor spelling it `bypass_call_guardrails` on the wire translates in its own adapter, which is what the boundary at the top of this file is for.
CAPABILITIES = (
	"templates", "media", "session", "buttons", "lists", "backfill", "recover_message", "recover_media",
	"recording", "bypass_guardrails",
)

# How a provider spells a phone number ON THE WIRE. `+919876543210`, `919876543210` and `9876543210` are the same subscriber and no two providers agree on which to accept — WATI puts the number in a URL query (`?whatsappNumber=`), where a `+` decodes as a space; a voice provider wants the `+`. The format is therefore a fact about the PROVIDER and it is declared, never resolved centrally.
E164_PLUS = "e164_plus"
E164_PLAIN = "e164_plain"
NATIONAL = "national"

# `NATIONAL` is declared by no adapter today, and it exists on purpose. Without it "a country code is required" would be a global engine rule wearing a declaration's clothes — which is the fix that was rejected — and the refusal below could never be proven to be the ADAPTER's rather than the engine's. `CAPABILITIES` carries `buttons`/`lists` on the same footing: a vocabulary is the space of expressible requirements, not an inventory of today's providers.
NUMBER_FORMATS = (E164_PLUS, E164_PLAIN, NATIONAL)

# Separators a human or an importer may have typed. Everything else — most of all the leading `+` — is SIGNAL and is never stripped.
_SEPARATORS = re.compile(r"[\s\-().]")
_WITH_COUNTRY_CODE = re.compile(r"^\+(\d{8,15})$")
_SUBSCRIBER_ONLY = re.compile(r"^(\d{4,15})$")


class SendResult(tuple):
	"""The one shape every send returns, over THREE outcomes — accepted, refused, unknown.

	`correlation_id` is the id the provider's own status events will echo — never a second id it merely
	happens to mint, because a status that echoes nothing this row carries can never tick it.

	`unknown` is a send that got no answer: a timeout, a dropped connection. It is not `accepted`, and
	it is emphatically not a refusal — the message may already be on the patient's phone. Collapsing it
	into refused is what makes a rep press Send twice; collapsing it into accepted hides a real
	non-delivery. A caller must not retry an unknown send, because nothing on the wire can collapse the
	duplicate.
	"""

	__slots__ = ()

	def __new__(cls, accepted, correlation_id=None, error=None, unknown=False, wamid=None):
		return tuple.__new__(cls, (bool(accepted), correlation_id, error, bool(unknown), wamid))

	@property
	def accepted(self):
		return self[0]

	@property
	def correlation_id(self):
		return self[1]

	@property
	def error(self):
		return self[2]

	@property
	def unknown(self):
		return self[3]

	@property
	def wamid(self):
		"""The provider's WhatsApp message id, when it gave one. A SECOND identity, never a substitute for
		`correlation_id`: only the correlation id is echoed by status events, while only the wamid is what
		an inbound button tap points back at through `replyContextId`. Two questions, two keys."""
		return self[4]

	def __repr__(self):
		return (
			f"SendResult(accepted={self.accepted!r}, correlation_id={self.correlation_id!r}, "
			f"error={self.error!r}, unknown={self.unknown!r}, wamid={self.wamid!r})"
		)


class RecordingRef(tuple):
	"""WHERE a call's audio is — the whole of what a `recording` adapter ever says about it.

	THREE ANSWERS, and the difference between them is the reason this is not just a URL string:

	  here it is    `RecordingRef(url=..., provider=...)` — fetch it and it is ours
	  not ready yet `RecordingRef(pending=True)` — the call is real, the audio is not published yet
	  there is none `RecordingRef()` — this call produced no audio and never will

	Collapsing "not ready" into "none" is what makes a recording silently never arrive; collapsing it into
	"here it is" makes the fetcher hammer a 404. They are separately routable states on the media row.

	`provider` is DATA. It rides in from the adapter so `call_media` can stamp a producer onto the file
	name without ever learning a vendor exists — the shared service formats a string it was handed.

	`headers` is whatever authentication the fetch needs (a bearer token, a signed header). Most providers
	publish an unauthenticated URL and send none; a provider that needs one declares it here rather than
	teaching the shared fetcher about its auth.

	`allowed_hosts` is the same shape of declaration for the SSRF guard: the operator-configured hosts this
	provider's recordings may be fetched from. It is DERIVED, never stored — a copy of operator config on a
	data row would keep answering with the allowlist the operator has since narrowed. Empty means the
	generic guard alone, which every hop runs regardless: http(s) only, and no host that resolves off the
	public internet.
	"""

	__slots__ = ()

	def __new__(cls, url=None, provider=None, headers=None, pending=False, allowed_hosts=None):
		return tuple.__new__(cls, (url or None, provider or None, dict(headers or {}), bool(pending),
		                           tuple(allowed_hosts or ())))

	@property
	def url(self):
		return self[0]

	@property
	def provider(self):
		return self[1]

	@property
	def headers(self):
		return self[2]

	@property
	def pending(self):
		"""The provider will have audio for this call, but not yet. The media row waits rather than closing."""
		return self[3]

	@property
	def allowed_hosts(self):
		"""Hosts this recording may be fetched from, or empty for the generic public-internet guard alone."""
		return self[4]

	@property
	def absent(self):
		"""Settled: there is no audio for this call. Not the same as `pending`, and never guessed from a
		missing URL alone — an adapter says this by answering `RecordingRef()` on a terminal payload."""
		return not self[0] and not self[3]

	def __repr__(self):
		return (
			f"RecordingRef(url={self.url!r}, provider={self.provider!r}, "
			f"headers={sorted(self.headers)!r}, pending={self.pending!r}, "
			f"allowed_hosts={list(self.allowed_hosts)!r})"
		)


@dataclass(frozen=True)
class Declaration:
	"""One adapter's self-description. Frozen: a declaration is read everywhere and owned nowhere else."""

	channel: str
	provider: str
	account_doctype: str
	outcomes: frozenset
	capabilities: frozenset
	number_format: str

	def __post_init__(self):
		unknown = set(self.outcomes) - set(OUTCOMES)
		if unknown:
			raise ValueError(f"{self.provider}: undeclarable outcome(s) {sorted(unknown)}; known: {list(OUTCOMES)}")
		unknown = set(self.capabilities) - set(CAPABILITIES)
		if unknown:
			raise ValueError(f"{self.provider}: unknown capability/ies {sorted(unknown)}; known: {list(CAPABILITIES)}")
		if self.number_format not in NUMBER_FORMATS:
			raise ValueError(
				f"{self.provider}: unknown number format {self.number_format!r}; known: {list(NUMBER_FORMATS)}"
			)

	def emits(self, outcome) -> bool:
		"""Can this provider truthfully report this outcome?"""
		return outcome in self.outcomes

	def can(self, capability) -> bool:
		"""Does this provider have this capability?"""
		return capability in self.capabilities

	def conform_number(self, number) -> str | None:
		"""This number as THIS provider spells it, or None when it cannot be known to be right.

		THE DEFECT THIS EXISTS TO DELETE. A lead's number was stored as a bare Indian 10-digit and the
		send path reduced it with a `\\D`-strip that called itself E.164. WATI resolved the dialling plan
		itself, read the leading digits as a country code, and the message reached a different subscriber
		in another country. Nothing in the system had declared a country, so the PROVIDER guessed one.

		The leading `+` is the ONLY in-band evidence that a country code is present: `919876543210` and
		`9876543210` are both just digits, and deciding which is which means guessing a dialling plan.
		So the judgement is symmetric and nothing is inferred in either direction — a `+`-carrying number
		satisfies the two E.164 spellings and is REFUSED for `NATIONAL`, because a country code cannot be
		stripped off without knowing how many digits it occupies; a plus-less number satisfies `NATIONAL`
		alone. Separators are cosmetic and are removed; the `+` never is.

		Refusal is the point, not a fallback: an ambiguous number is not sent at all. The caller turns
		None into a routable `failed` outcome (`automation.sends.send_whatsapp`) so a badly stored number
		is a data state the author routes on, never an exception that kills a journey.

		A blank number returns None too, but no send path reaches this with one — `send_whatsapp` refuses
		a missing `mobile_no` several lines earlier, with its own distinct marker, so "no number at all"
		and "a number this provider cannot dial" stay two separately routable facts in the step log.
		"""
		bare = _SEPARATORS.sub("", number or "")
		if self.number_format == NATIONAL:
			subscriber = _SUBSCRIBER_ONLY.match(bare)
			return subscriber.group(1) if subscriber else None
		qualified = _WITH_COUNTRY_CODE.match(bare)
		if not qualified:
			return None
		return qualified.group(1) if self.number_format == E164_PLAIN else "+" + qualified.group(1)

	def as_dict(self) -> dict:
		"""The declaration as plain data — what the chat UI and the config screens read."""
		return {
			"channel": self.channel,
			"provider": self.provider,
			"account_doctype": self.account_doctype,
			"outcomes": sorted(self.outcomes),
			"capabilities": sorted(self.capabilities),
			"number_format": self.number_format,
		}


def declare(*, channel, provider, account_doctype, outcomes, capabilities, number_format) -> Declaration:
	"""Build a declaration. Called once per adapter, at module scope, so an adapter that claims an
	outcome the vocabulary does not have — or a number format nobody defined — fails at IMPORT, not on
	the one payload that needed it. Every argument is required: a defaulted number format would be this
	module quietly deciding an address rule on a provider's behalf, which is the whole defect."""
	return Declaration(
		channel=channel,
		provider=provider,
		account_doctype=account_doctype,
		outcomes=frozenset(outcomes),
		capabilities=frozenset(capabilities),
		number_format=number_format,
	)
