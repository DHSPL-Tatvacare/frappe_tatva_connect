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

`normalize` and `normalize_history` are TWO PRODUCERS OF ONE ENVELOPE — the webhook shape and the
provider's history shape both come out as a `ChannelEvent`, and nothing downstream can tell which was
which. That is the whole boundary: a vendor's field names stop here.

The declaration is the single source of truth, read by the ingress, the send path and the chat UI.
`outcomes` is deliberately a subset the adapter can TRUTHFULLY emit: a provider that cannot tell a
read from a delivery must not be allowed to claim `read`, because a consumer downstream will believe
it. `capabilities` is the same promise for the send side — the UI hides a template picker for a
provider that has no templates rather than offering a button that will throw.
"""
from dataclasses import dataclass

from tatva_connect.channels.event import OUTCOMES

# What a provider can DO, as opposed to what it can REPORT (that is `outcomes`). The first six are send-side. The last two are READ-side, and they are what makes an orphan status recoverable: `recover_message` = "I can hand back the ONE message a status names", `recover_media` = "and that message's file, by its id". A provider that declares neither is not broken — its orphan statuses are logged and dropped, which is what happened to every provider before either existed.
CAPABILITIES = (
	"templates", "media", "session", "buttons", "lists", "backfill", "recover_message", "recover_media",
)


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

	def __new__(cls, accepted, correlation_id=None, error=None, unknown=False):
		return tuple.__new__(cls, (bool(accepted), correlation_id, error, bool(unknown)))

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

	def __repr__(self):
		return (
			f"SendResult(accepted={self.accepted!r}, correlation_id={self.correlation_id!r}, "
			f"error={self.error!r}, unknown={self.unknown!r})"
		)


@dataclass(frozen=True)
class Declaration:
	"""One adapter's self-description. Frozen: a declaration is read everywhere and owned nowhere else."""

	channel: str
	provider: str
	account_doctype: str
	outcomes: frozenset
	capabilities: frozenset

	def __post_init__(self):
		unknown = set(self.outcomes) - set(OUTCOMES)
		if unknown:
			raise ValueError(f"{self.provider}: undeclarable outcome(s) {sorted(unknown)}; known: {list(OUTCOMES)}")
		unknown = set(self.capabilities) - set(CAPABILITIES)
		if unknown:
			raise ValueError(f"{self.provider}: unknown capability/ies {sorted(unknown)}; known: {list(CAPABILITIES)}")

	def emits(self, outcome) -> bool:
		"""Can this provider truthfully report this outcome?"""
		return outcome in self.outcomes

	def can(self, capability) -> bool:
		"""Does this provider have this capability?"""
		return capability in self.capabilities

	def as_dict(self) -> dict:
		"""The declaration as plain data — what the chat UI and the config screens read."""
		return {
			"channel": self.channel,
			"provider": self.provider,
			"account_doctype": self.account_doctype,
			"outcomes": sorted(self.outcomes),
			"capabilities": sorted(self.capabilities),
		}


def declare(*, channel, provider, account_doctype, outcomes, capabilities) -> Declaration:
	"""Build a declaration. Called once per adapter, at module scope, so an adapter that claims an
	outcome the vocabulary does not have fails at IMPORT — not on the one payload that needed it."""
	return Declaration(
		channel=channel,
		provider=provider,
		account_doctype=account_doctype,
		outcomes=frozenset(outcomes),
		capabilities=frozenset(capabilities),
	)
