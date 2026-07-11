"""Per-provider telephony adapters — the ONLY code that knows a provider's field names.

Nomenclature, matching the rest of the app: a PROVIDER is the company (Acefone, Ozonetel —
the value in `CRM Telephony Account.provider`); an ADAPTER is the module that speaks to it.
One module per provider, named for the provider.

Each adapter implements the webhook-spine contract (`tatva_connect.webhooks.spine`):

    is_relevant(payload, event, account)       -> cheap front-door pre-filter
    already_processed(payload, event, account) -> idempotency vs CRM Call Log
    handle(payload, event, account)            -> parse + write
    account_for_payload(payload, event)        -> re-derive the account on replay

plus the one function that makes it multi-provider:

    normalize(payload, event, account) -> Envelope | None

Returning None means "not a call we can key". The spine raw-logs every payload BEFORE the
adapter sees it, so a None is never a loss — only a decision not to build a Call Log from it.

Everything after `normalize` is shared and provider-blind: `telephony.envelope` defines the
interface, `telephony.writer` does the DB moves. Adding a provider is a module in here and
nothing else.

(WhatsApp has a single provider, so it stays `whatsapp/adapter.py`. Telephony has more than
one, so its adapters live here, keyed by provider name. Same rule, one level deeper.)
"""
