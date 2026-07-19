"""Per-provider telephony adapters — the only code that knows a provider's field names.

A PROVIDER is the company (the value in `CRM Telephony Account.provider`); an ADAPTER is the module
that speaks to it. One module per provider, named for the provider.

Each adapter implements the webhook-spine contract — `screen`, `already_processed`, `handle`,
`account_for_payload` — plus the one function that makes it multi-provider:

    normalize(payload, event, account) -> Envelope | None

None means the payload carries no usable key. The spine raw-logs every payload before an adapter is
called, so a None is never a loss, only a decision not to build a Call Log from it.

Everything after `normalize` is shared: `telephony.envelope` defines the interface, `telephony.resolve`
answers the gates, `telephony.writer` does the DB moves.

WhatsApp keys its adapters the same way, at `whatsapp/wati.py`. Both channels are reached through
`channels.resolve` — the account row names the vendor, and nothing else does.
"""
