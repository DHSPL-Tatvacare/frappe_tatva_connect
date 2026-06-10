# Acefone telephony integration (`tatva_connect.acefone`)

Logs phone calls into the CRM and lets agents click-to-call a lead — all through **Acefone** (our cloud telephony provider), via **clean backend overrides only, no crm fork**. Sibling of the WATI WhatsApp integration: same app, same dev-first discipline, same multi-account routing.

Acefone API mechanics were adapted from the MIT-licensed [`sanskar-onehash/crm_acefone_integration`](https://github.com/sanskar-onehash/crm_acefone_integration); the write target was changed to crm's **native `CRM Call Log`** so calls render in the lead's Calls tab with no glue.

## Binding rule
**No `frappe/crm` (or any app) source edits.** Everything is `override_whitelisted_methods` + config + our own `tatva_connect` code.

## The core idea: ride the native call UI

crm's call UI (the **phone icon** on a lead/deal/contact, "Make a Call", the call popup, the inline **"Listen"** recording player) only appears when a telephony integration's settings are *enabled* (`callEnabled` = `is_call_integration_enabled()` returns any true). Acefone is **not** in crm's hardcoded set (Twilio/Exotel only), and there is **no provider-registration hook**. So we:

1. **Enable the Exotel "slot"** (CRM Exotel Settings → Enabled; no Exotel creds needed) → the native phone icon lights up everywhere.
2. **Override the two backend methods** that UI calls, so the native UI drives **Acefone**:

| crm method (replaced) | our handler (`acefone/bridge.py`) | effect |
|---|---|---|
| `crm.integrations.exotel.handler.make_a_call` | `make_a_call` | the native phone icon places an **Acefone bridge call** (account chosen by routing) |
| `crm.fcrm.doctype.crm_call_log.crm_call_log.get_call_log` | `get_call_log` | for Acefone calls, overwrites `recording_url_path` with our streaming proxy → the native **"Listen"** player works inline |

The CRM never reaches Exotel — `make_a_call` is fully replaced. `get_call_log` delegates to the original crm function and only augments Acefone rows (Twilio/Exotel untouched).

> **What is a "bridge"?** Acefone calls the agent's phone first, then dials the patient, then connects (bridges) the two real phone lines. The audio is on the phones, never in the browser — so a true in-browser softphone/dialer is **impossible** for Acefone (that's a WebRTC/Twilio-only thing). The most any bridge provider gets is a status popup. (Acefone *does* have a WebRTC softphone, but only as their standalone AceConnect app / Chrome extension — **no embeddable SDK**, so it can't live inside our CRM.)

## What it does

1. **Inbound + call logging** — Acefone POSTs call events (CDRs) to our webhook → we create/update a `CRM Call Log` (`telephony_medium="Acefone"`), match the caller's number to a lead, link it. Renders in the Calls tab automatically.
2. **Outbound click-to-call** — native phone icon → `make_a_call` → resolve account by routing → Acefone `click_to_call` → log the call. No sync call id, so we correlate the later webhook via a `custom_identifier` passthrough (fallback: number + recency).
3. **Recordings** — play **inline** in the Calls tab via the native "Listen" badge; streamed on demand through our proxy (no storage — only the URL lives on the Call Log).
4. **Multi-account routing** — multiple Acefone accounts, routed by the lead's Product Line / Group / Program (same model as WATI).
5. **Kill-switch** — `Acefone Settings → Enabled`. Off = no calls, no inbound logging.

## Folder / file map

```
tatva_connect/acefone/
├── api.py        Acefone HTTP client. Account-driven (each call takes an Acefone
│                 Account doc → its base_url + Bearer api_token). click_to_call(),
│                 get_all_messages() (CDR pull), global kill-switch helpers.
├── bridge.py     The two crm overrides (make_a_call, get_call_log) + small helpers.
│                 THIS is the native-UI piggyback.
├── handler.py    Inbound webhook (4 guest endpoints, one per Acefone trigger) ->
│                 _process -> CRM Call Log (idempotent on uuid). make_acefone_call
│                 is a thin by-reference API wrapper that delegates to bridge.
├── routing.py    Pick the Acefone account for a lead (outbound) / a DID (inbound).
│                 Most-specific wins (Program > Group > Product Line); no global
│                 default (unrouted outbound is blocked).
└── README.md     this file

tatva_connect/api/telephony.py   recording(call_log) — streams ONE recording on
                 demand (proxied with the account token; nothing stored). Used by
                 the get_call_log override.

tatva_connect/tatva_connect/doctype/
├── acefone_account/          one record per account (creds + agent number + DID)
├── acefone_account_routing/  taxonomy -> account rules (+ duplicate guard)
└── acefone_settings/         global single: kill-switch + webhook secret
```

Native `CRM Call Log` gains a Custom Field `custom_acefone_account` (which account handled the call) and an "Acefone" option on `telephony_medium` (Property Setter). `CRM Telephony Agent` gains `acefone_number` (the agent's originating line).

## Vendor API (Acefone) — what we use
- Base `https://api.acefone.in/v1/`, auth **Bearer** (token from dashboard → API Connect → API Tokens; ask for a long-life token).
- `POST /v1/click_to_call` — `{agent_number, destination_number, async:"1", caller_id?, custom_identifier?}` → `{success, message}` (no sync call id).
- **Webhooks** (API Connect → Webhook), one per trigger (answered / hangup, inbound / outbound). CDR fields we read (confirmed via the OneHash integration): `uuid`, `call_id`, `customer_number`, `did_number`, `direction`, `call_status`, `recording_url`, `duration`, `start_stamp`/`answer_stamp`/`end_stamp`, `answered_agent_number`, `hangup_cause`, `custom_identifier`.
- `recording_url` arrives **in the hangup CDR** (also pullable via `GET /v1/call/records`).
- Docs: https://docs.acefone.in/

## Setup (multi-account)
1. **Acefone Account** (Desk → New): account_name, enabled, base_url (`https://api.acefone.in`), api_token, agent_number, caller_id (DID). Or via API: `POST /api/resource/Acefone Account`.
2. **Acefone Account Routing** rules: vertical / psp_group / program → Acefone Account. Most-specific wins; duplicates rejected; no global default.
3. **CRM Telephony Agent** → set each agent's `acefone_number`.
4. **Enable the Exotel slot** (CRM Exotel Settings → Enabled) so the phone icon appears, and **Acefone Settings → Enabled** (kill-switch on).
5. **Register webhooks** per account: `https://<host>/api/method/tatva_connect.acefone.handler.{inbound_answered,inbound_complete,outbound_answered,outbound_complete}?key=<Acefone Settings webhook_verify_token>`.

## Credentials to provide (to go live)
1. **API Token** (long-life) → `Acefone Account.api_token`.
2. **Agent number** → `CRM Telephony Agent.acefone_number`.
3. **A DID** → `Acefone Account.caller_id`.

## Quirks / things to know
- **No browser dialer** — Acefone is a bridge; only a status popup is possible (physics, not our limitation).
- **Exotel slot is enabled but never used** — it's only there to turn on `callEnabled`; `make_a_call` is fully overridden, so no Exotel call is ever placed. The medium dropdown shows "Exotel" cosmetically.
- **Recordings: crm's `get_call_log` always points `recording_url_path` at its own Twilio/Exotel-only proxy**, so our override must **overwrite** it for Acefone — not just fill when empty.
- **Triple error toasts** — a frappe-ui quirk (its `frappeRequest` calls `onError` twice + re-throws), so one backend error shows ~3 toasts. Affects all of crm, not just us; not fixable without forking the frontend bundle. Left as-is.
- **Calls tab does not auto-refresh** after a call — crm only has a `whatsapp_message` socket listener, none for calls. A new call appears on reload. (No-fork fix possible via a form-script socket listener; not yet wired.)
- **Webhook accepts requests when no verify-token is set** (to allow a first capture) — set the token before prod.
- **Open items to verify on real creds:** exact `start_stamp` format / `duration` unit, whether `custom_identifier` round-trips, and whether the recording URL needs the Bearer token (the proxy already sends it).
