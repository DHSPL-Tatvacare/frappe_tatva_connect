"""Which adapter speaks for this account — read from the ONE registry, never re-derived.

There used to be two registries: `webhooks/registry.py` keyed by vendor for the inbound half, and
`whatsapp/providers.py` keyed by vendor for the outbound half. Two lists of the same fact drift, and
the second one was already deriving a provider from a URL host substring because it did not trust the
first. There is now one declaration (`webhooks.registry.CHANNELS`) and this module reads it.

No silent default: an account whose provider has no registered adapter raises, so a misconfigured
account fails loud instead of sending through the wrong API.

The account ROW names the vendor. A URL host is a deployment detail, not an identity, and guessing a
provider from one is how a Meta account once looked like a WATI one. There is one way to reach a
declaration — `adapter_for(account).DECLARATION` — deliberately not a second helper.
"""
import frappe
from frappe import _

from tatva_connect.webhooks import registry


def _account_doc(account, account_doctype=None):
	"""Accept a doc or a name. A name needs the doctype, which the registry knows."""
	if isinstance(account, str):
		if not account_doctype:
			raise ValueError("resolving an adapter from an account NAME needs its doctype")
		return frappe.get_cached_doc(account_doctype, account)
	return account


def has_adapter(account, account_doctype=None) -> bool:
	"""True if this account maps to a registered adapter."""
	doc = _account_doc(account, account_doctype)
	cfg = registry.by_account_doctype(doc.doctype)
	if not cfg:
		return False
	return (doc.get(cfg["provider_field"]) or "") in cfg["adapters"]


def adapter_for(account, account_doctype=None):
	"""The adapter module for this account, or raise. Lazy-imported, so no import cycle."""
	doc = _account_doc(account, account_doctype)
	cfg = registry.by_account_doctype(doc.doctype)
	if not cfg:
		frappe.throw(
			_("No channel is registered for account doctype '{0}'.").format(doc.doctype),
			title=_("Unknown channel"),
		)
	provider = doc.get(cfg["provider_field"]) or ""
	return adapter_for_channel(cfg["channel"], provider, account_hint=getattr(doc, "name", doc))


def adapter_for_channel(channel, provider, account_hint=None):
	"""The adapter module for a (channel, provider) pair, or raise."""
	cfg = registry.by_channel(channel)
	if not cfg:
		frappe.throw(_("No channel registered as '{0}'.").format(channel), title=_("Unknown channel"))
	path = cfg["adapters"].get(provider)
	if not path:
		frappe.throw(
			_("No {0} adapter for provider '{1}'{2}.").format(
				channel, provider or _("(unset)"), f" on account '{account_hint}'" if account_hint else ""
			),
			title=_("Unknown provider"),
		)
	return frappe.get_module(path)


def outcomes_for_channel(channel) -> list:
	"""Every outcome ANY registered adapter on this channel can truthfully report, as signal names.

	The workflow node that sends on a channel offers exactly these as waitable events, so an author
	branches on `delivered` because WATI DECLARED it can report `delivered` — never because someone typed
	the word into a node type.

	The UNION, deliberately, not the intersection. An adapter is only known at send time (the account is
	resolved from the lead's grain), while the canvas and the publish gate ask at AUTHORING time, with no
	lead in hand. The union is the honest static answer to "what can a send on this channel ever report".
	Its cost is real and accepted: with two providers of unequal ability an author may name an outcome the
	routed provider cannot report, and that Wait then leaves by its timeout edge — which is what the
	timeout edge is for. The intersection would have hidden WATI's real `clicked` behind a poorer future
	provider, impoverishing every graph to protect a case that already has an answer.

	Namespaced by channel, matching `task.completed`: a bare `delivered` would collide the moment a second
	channel reports one.
	"""
	cfg = registry.by_channel(channel)
	if not cfg:
		frappe.throw(_("No channel registered as '{0}'.").format(channel), title=_("Unknown channel"))
	found = set()
	for path in (cfg.get("adapters") or {}).values():
		found.update(frappe.get_module(path).DECLARATION.outcomes)
	return sorted(f"{channel}.{outcome}" for outcome in found)


def adapter_for_payload(channel, payload, event=None):
	"""The adapter that OWNS a payload, and the account it names. Returns (adapter, account).

	A LIVE delivery is identified by the token in its URL: the token resolves to one account, and the
	account row names the vendor. A REPLAYED delivery has no token — it is deliberately never persisted,
	so there is nothing to read back, and storing one to make replay work would be trading a secret for
	a convenience.

	So the same question is asked of the payload instead. Each of the channel's adapters is offered it
	and the first to recognise it as its own owns it — through `account_for_payload`, the hook every
	adapter already implements for exactly this purpose. No second identity rule, and no vendor guessed
	from a URL host or from being the only one currently installed.

	(None, None) when nothing claims it. The caller decides what that means, because running one
	vendor's parser over another's payload is worse than declining to replay.
	"""
	cfg = registry.by_channel(channel)
	if not cfg:
		frappe.throw(_("No channel registered as '{0}'.").format(channel), title=_("Unknown channel"))
	for path in (cfg.get("adapters") or {}).values():
		adapter = frappe.get_module(path)
		resolver = getattr(adapter, "account_for_payload", None)
		if not resolver:
			continue
		try:
			account = resolver(payload, event)
		except Exception:
			# A foreign payload may raise in another vendor's resolver; the true owner still gets its turn.
			frappe.log_error(
				title=f"{channel}: adapter '{path}' raised while identifying a replayed payload",
				message=frappe.get_traceback(),
			)
			continue
		if account:
			return adapter, account
	return None, None
