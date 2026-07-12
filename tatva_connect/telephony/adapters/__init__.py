"""Per-provider telephony adapters — the only code that knows a provider's field names.

A PROVIDER is the company (the value in `CRM Telephony Account.provider`); an ADAPTER is the module
that speaks to it. One module per provider, named for the provider.

Each adapter implements the webhook-spine contract — `is_relevant`, `already_processed`, `handle`,
`account_for_payload` — plus the one function that makes it multi-provider:

    normalize(payload, event, account) -> Envelope | None

None means the payload carries no usable key. The spine raw-logs every payload before an adapter is
called, so a None is never a loss, only a decision not to build a Call Log from it.

Everything after `normalize` is shared: `telephony.envelope` defines the interface, `telephony.resolve`
answers the gates, `telephony.writer` does the DB moves.

WhatsApp has a single provider and stays at `whatsapp/adapter.py`. Telephony has more than one, so its
adapters live here, keyed by provider name. The same rule, one level deeper.
"""
