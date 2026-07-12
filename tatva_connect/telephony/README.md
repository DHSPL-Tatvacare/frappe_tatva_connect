# Telephony (`tatva_connect.telephony`) — Acefone today, any provider tomorrow

Logs phone calls onto the lead and lets an agent click-to-call, through **clean backend overrides only,
no crm fork**. Acefone is the first provider; the module is shaped so the second one writes an adapter
and nothing else.

Acefone API mechanics were adapted from the MIT-licensed
[`sanskar-onehash/crm_acefone_integration`](https://github.com/sanskar-onehash/crm_acefone_integration);
the write target was changed to crm's **native `CRM Call Log`** so calls render in the lead's Calls tab
with no glue.

## Binding rule

**No `frappe/crm` (or any app) source edits.** Everything is `override_whitelisted_methods` + config +
our own `tatva_connect` code.

## Two front doors, one brain

A call reaches the CRM two ways, and the distinction is deliberate.

```
PUSH   Acefone POSTs a CDR
       -> handler.py            4 guest endpoints, one per trigger; direction rides in the URL
       -> webhooks/spine.py     authenticate -> kill-switch -> screen -> raw-log -> ACK -> enqueue
                                  |
PULL   we GET /v1/call/records   |
       -> reconcile.py          maps the record into the provider's own webhook vocabulary
                                  |
                                  v
       -> adapters/acefone.py :: process()      THE SHARED ENTRY
                                  screen  -> is this call ours?        (resolve.py)
                                  normalize -> one Envelope            (envelope.py)
                                  write   -> CRM Call Log              (writer.py)
```

Everything below `process()` is provider-blind and is asked the same questions whether the call was
pushed or pulled — the gates, the field mapping, the lead-linking, the status, the agent, the recording.
A pulled call therefore cannot disagree with the pushed one.

The **front doors stay separate on purpose.** A webhook arrives over HTTP from an untrusted caller, so
it is authenticated, raw-logged as an `Integration Request` and made replayable. A record is fetched by
us, with our own token, from the source of truth: there is no delivery to authenticate and none to
replay, and routing the pull through the webhook spine would fabricate both. Sharing the *front door*
is not the goal; sharing the *logic* is.

## Resolution — two factors, fail-closed

The Acefone tenant is **shared** with other businesses (Visit, ICICI Lombard, Quest, Star AHC): 19 DIDs
across four-plus companies interleave on one account. So relevance is decided before anything is
written.

- The **token authenticates the account.** The **DID selects the grain** (`CRM Telephony DID`).
- **A DID that is not mapped is dropped**, and the reason is written onto the row. No best-guess
  attribution, ever — that is what keeps another company's customer PII out of this CRM.
- The **lead** is matched on `(DID -> grain) + phone`, so one phone number across several leads still
  attributes to the right one.
- The **agent** is matched on email: a corporate address auto-resolves, anything else needs a
  `CRM Telephony Agent Map` row. An unmapped agent leaves the rep **blank** and the call is still kept —
  relevance and attribution are separate questions. Answered-but-unattributed renders as **External**.
- **`CRM Telephony Capture Rule`** decides which direction/channel combinations are captured at all. An
  empty rule table captures nothing.

## Folder / file map

```
tatva_connect/telephony/
├── adapters/acefone.py  THE ONLY Acefone-specific code, and the template for the next provider.
│                        screen / already_processed / handle / account_for_payload / normalize.
│                        process() is the shared entry the webhook worker AND reconcile both call.
├── envelope.py   The provider-neutral Envelope every adapter emits. One interface.
├── resolve.py    The gates: account_for_did · grain_for · should_capture · lead_for · user_for.
├── writer.py     Envelope -> CRM Call Log. Branches once, on direction.
├── api.py        Acefone HTTP client, account-driven (base_url + Bearer api_token per account).
│                 click_to_call() · get_call_records() (the CDR pull) · kill-switch helpers.
├── handler.py    The 4 inbound guest endpoints -> spine.receive(). Plus make_acefone_call.
├── reconcile.py  The API pull. Same adapter entry, its own front door. Manual, dry-run by default.
├── providers.py  OUTBOUND registry: provider -> the module that speaks its API. Used by bridge.py.
├── routing.py    OUTBOUND only: pick the account for a lead's grain. Most-specific wins, no default.
├── bridge.py     The two crm overrides that make the native call UI drive Acefone.
└── permissions.py

tatva_connect/webhooks/          shared by Acefone AND WATI — not a telephony thing
├── spine.py      The one webhook front door: auth -> kill-switch -> screen -> log -> ACK -> enqueue.
│                 Plus replay() / replay_service() — the DLQ is a button.
├── ingress.py    The one auth gate: token by SHA-256 digest, optional HMAC, optional IP allowlist.
├── registry.py   INBOUND registry: service -> adapter, account doctype, token field, URL builder.
└── urls.py

tatva_connect/api/telephony.py   recording(call_log) — streams one recording on demand, nothing stored.
```

`providers.py` and `webhooks/registry.py` are both provider maps and are deliberately **not** merged:
one picks the module that speaks a provider's API when the CRM places a call, the other picks the module
that reads a provider's payload when the provider calls us. Different questions, different data.

## Doctypes

| Doctype | What the operator puts in it |
|---|---|
| `CRM Telephony Account` | One per provider account: creds, `webhook_token`, HMAC/IP settings. |
| `CRM Telephony DID` | **DID -> grain.** The relevance gate. Unmapped = dropped. |
| `CRM Telephony Agent Map` | Agent email -> CRM user, for agents whose email does not auto-resolve. |
| `CRM Telephony Capture Rule` | Which direction/channel is captured. Child of Settings. Empty = nothing. |
| `CRM Telephony Routing` | Grain -> account, for **outbound** click-to-call only. |
| `CRM Telephony Settings` | The kill-switch, rate limits, capture rules. |

`CRM Call Log` gains a Custom Field `custom_telephony_account` and an "Acefone" option on
`telephony_medium` (Property Setter).

## The native call UI (outbound)

crm's phone icon only appears when a telephony integration is enabled, and Acefone is not in crm's
hardcoded set (Twilio/Exotel only), with no provider-registration hook. So the **Exotel slot** is enabled
(no Exotel creds needed) to light up the icon, and the two backend methods it calls are overridden:

| crm method (replaced) | ours (`bridge.py`) | effect |
|---|---|---|
| `crm.integrations.exotel.handler.make_a_call` | `make_a_call` | the phone icon places an **Acefone** bridge call |
| `…crm_call_log.get_call_log` | `get_call_log` | points the native "Listen" player at our streaming proxy |

No Exotel call is ever placed. `get_call_log` delegates to crm's and only augments Acefone rows.

> **A bridge is not a softphone.** Acefone rings the agent's phone, then dials the patient, then connects
> the two real lines. The audio is on the phones, never the browser — an in-browser dialer is impossible
> for a bridge provider. The most any of them supports is a status popup.

## What the live traffic proved (and the docs got wrong)

The first adapter was written from Acefone's documentation and a 363-CDR capture disproved it:

- **`answered_agent_email` does not exist.** The email sits inside `answered_agent`, an **array**. The
  old code resolved zero agents.
- **`answered_agent_number` is an extension** (`Extension-0602141810277`), not a phone. It was being fed
  to a phone matcher, where it could never match and could collide on a 10-digit suffix.
- **`custom_identifier` and `ref_id` are empty on every payload.** There is **no correlation key** from a
  placed call back to its CDR. Outbound-from-CRM cannot be built on one.
- **`hangup_cause` never says "busy" or "cancel"** — both documented status branches were unreachable.
- **`call_status` is lowercase** despite the docs.
- **Acefone speaks two dialects on one webhook.** IVR `inbound` and `Dialer (inbound)` differ in
  timestamp format, phone format and hangup vocabulary. One provider already needs normalization — which
  is the whole argument for the envelope.
- **Every call a human answered was a Dialer call.** Plain IVR inbound has a 0% answer rate.
- **Duration is not talk time.** `duration` includes IVR time; `billsec` is empty on answered calls.
  Acefone exposes **no agent talk-time field**.
- **Acefone re-sends CDRs.** Keying the row on `call_id` handles it; `uuid` varies per leg and must not
  be the key.

## Known open items

- **Recordings do not play, and the fault is Acefone's.** `recording_url` returns **404 HTML
  server-side** — unauthenticated, with a Bearer token, and even when the URL is handed to us by
  Acefone's own authenticated API. Six URLs tested, all 404. Call recording is most likely not enabled on
  the account. With the provider.
- **The outbound `normalize()` branch has never seen a live payload.** No outbound webhook event was ever
  captured. It is written from the inbound corpus and the record API, not proven.
- **`scheduled_reconcile` is not wired** to `hooks.scheduler_events`, deliberately: anything that runs by
  itself needs a dormant automation toggle and a go-live checklist row first. Reconcile is manual today.
- **`CRM Telephony Routing` (outbound) and `CRM Telephony DID` (inbound) are two independent maps** and
  nothing validates that they agree.
- **`refresh_calls` has no button.** It is whitelisted and callable, but the Desk workspace does not
  surface it.

## Setup

1. **CRM Telephony Account** — provider, enabled, `base_url` (`https://api.acefone.in`), `api_token`.
2. **Generate the webhook token** on the account form and **register the four webhook URLs** on the
   Acefone dashboard (API Connect → Webhook), one per trigger. The token authenticates the caller and
   identifies the receiving account.
3. **CRM Telephony DID** — map every DID you own to its grain. **A DID that is missing here is dropped.**
4. **CRM Telephony Capture Rule** — say what to capture. **Empty captures nothing.**
5. **CRM Telephony Agent Map** — only for agents whose email does not auto-resolve.
6. Turn on the **`Telephony::Acefone::calls`** switch. For outbound, also enable the Exotel slot and add
   **CRM Telephony Routing** rules.
