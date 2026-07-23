# The WhatsApp channel (`tatva_connect.whatsapp`)

This folder makes the CRM send and receive WhatsApp through a **declared channel adapter**.
WhatsApp is the CHANNEL; **WATI is the first vendor on it**, and today the only one. Its
chatbot flows and approved templates live on the vendor side. The single hard rule
everything below protects:

> **No code path may ever reach Meta's WhatsApp API — not once.**

Nothing outside `wati.py` and `transport.py` knows the vendor's name. The route carries no
vendor, the switch keys carry no vendor, the registry is keyed by channel, and an emitted
event is `whatsapp.delivered`, never `wati.delivered`. Adding a second provider is: write an
adapter module that declares itself and implements the surface in `channels/contract.py`, add
one line to `webhooks/registry.py`, add the Select option. No call site changes.

## How it plugs in (no CRM fork, no patches)

The CRM uses the open-source `frappe_whatsapp` app, which talks to **Meta** out of
the box. We do **not** edit `frappe_whatsapp` or `frappe/crm`. Instead `tatva_connect`
**overrides** the three pieces that would otherwise call Meta, via `hooks.py`:

| What | How it's redirected |
|---|---|
| `WhatsApp Message` (every manual / picker send) | `override_doctype_class` → `ChannelWhatsAppMessage` |
| `WhatsApp Notification` (automated / scheduled sends) | `override_doctype_class` → `ChannelWhatsAppNotification` |
| `WhatsApp Templates` (create / edit / delete / fetch) | `override_doctype_class` → `ChannelWhatsAppTemplates` + `override_whitelisted_methods` for the "Sync" button |

Because these are framework-level overrides, the CRM's own UI and code stay
untouched — they just write a `WhatsApp Message` row like always, and our override
sends it via WATI. The agent's experience is the stock CRM, plus our template picker.

## What the integration does

**1. Outbound — three kinds of send**
- **Template** (anytime): the agent clicks **Send Template** on a lead → our picker
  opens (see Template variables below) → we POST to WATI `sendTemplateMessage`.
- **Session text** (free text, only inside WhatsApp's 24-hour window): `sendSessionMessage`.
- **Media / attachment**: the actual file bytes are uploaded to WATI
  (`sendSessionFile` / `sendSessionFileViaUrl`) — not the filename as text.

**2. Inbound — the webhook**
WATI POSTs the whole tenant's traffic to one guest endpoint. We verify a secret,
honour the kill-switch, drop everything that isn't a CRM lead, and only then store
the survivors (inbound customer messages + delivery/read statuses for messages we
sent). Inbound rows attach to the lead and show in its WhatsApp tab automatically.

**3. Templates**
Templates are **managed on WATI**, not in Frappe. We mirror them read-only into the
`WhatsApp Templates` doctype (the "Sync from WATI" button) so the picker has a list.
We never create, edit, or delete a template on the provider.

**4. Template variables (`{{1}}`, `{{2}}`…)**
When a template has variables, the picker shows the message with each slot
highlighted and gives the agent one input per slot — type a value, or pick a CRM
Lead / profile field from the dropdown. We send those values to WATI as named
params. This scales to any number of templates with **no per-template mapping**.

**5. Kill-switch**
`WATI Settings → Enabled`. When off, **both sending and receiving stop**. Default is
on (so a fresh install works), but the switch is checked fresh on every send and
every inbound event.

**6. Reconcile a lead's thread — the "Refresh WhatsApp" button**
On the CRM Lead header (leftmost, before Assign / Convert to Deal) there's a
**Refresh WhatsApp** button. Click it to pull the lead's **entire** WhatsApp
history from WATI (the v3 conversation read, all pages) and rebuild the thread — so any
discrepancy between what WATI has and what the CRM shows is rattled out.

It's built to be safe:
- **Scoped to that one lead.** It only ever touches that lead's rows — never any
  other lead's.
- **Authoritative + idempotent.** Each WATI message has a stable id; we key rows
  on it, so clicking twice gives the same result (no duplicates).
- **Never re-sends.** Rows are written directly to the database — clicking Refresh
  cannot fire a WhatsApp message.
- **Fetch-then-rebuild in one transaction.** If the WATI fetch fails, nothing is
  deleted; the old thread stays intact.
- Statuses (delivered/read/failed) and message times come straight from WATI.

**7. Scheduled template sync (every 6 hours)**
On top of the real-time manual sync, a scheduler job re-syncs **every** WATI
account's templates every 6 hours, so the local mirror is almost always current.
It respects the kill-switch and never crashes the scheduler on a sync error.
(Requires the bench scheduler to be enabled — it is on prod.)

## Folder / file map

```
tatva_connect/channels/   (vendor-free — see channels/__init__.py)
├── contract.py       What an adapter DECLARES (channel · provider · account doctype ·
│                     the outcomes it can TRUTHFULLY emit · its capabilities) and the
│                     one `SendResult` every send returns.
├── event.py          `ChannelEvent` — the ONE normalized event every adapter emits.
│                     Frozen, dict-like (so it serialises into the raw log unchanged).
│                     Canonical names: whatsapp.sent|delivered|read|replied|clicked|failed.
├── resolve.py        The adapter for an account, read from the ONE registry.

tatva_connect/whatsapp/
├── channel.py        Channel-level and vendor-free: the `WhatsApp::Channel::messaging`,
│                     `::templates`, `::backfill` and `::recovery` switches, and E.164
│                     canonicalisation.
├── wati.py           THE ADAPTER. Declares WATI, and implements normalize() +
│                     normalize_history() + the spine contract + send_template /
│                     send_session / send_media / list_templates / template_variables /
│                     history / recover_message / fetch_media_by_message_id.
│                     TWO normalizers, ONE envelope: the webhook is camelCase and the v3
│                     reads are snake_case, and that difference dies here.
├── transport.py      WATI HTTP client — the wire and nothing above it. One WATI tenant =
│                     one WhatsApp Account (base URL + JWT). Never raises on a WATI error
│                     body (returns it so callers show a clean message). Every SEND is v1
│                     and tenant-scoped; every READ is v3 and host-rooted — `base_url` is
│                     the one function that knows the two differ.
├── ingest.py         VENDOR-BLIND persistence. Reads a ChannelEvent and nothing else:
│                     attribution, the WhatsApp Message rows, status ticks, media filing.
│                     `apply` for a live event, `apply_historical` for one pulled out of
│                     the past (same writes, no entry triggers, no notifications).
├── recovery.py       Orphan-status recovery: a receipt for a message we never stored
│                     fetches THAT ONE message from its conversation, files it as
│                     backfill, and applies the receipt. Queued, surgical, idempotent,
│                     fail-soft, and dormant until `::recovery` is switched on.
├── message.py        ChannelWhatsAppMessage — overrides WhatsApp Message. Sends through
│                     the account's adapter (template / session / media), resolves the
│                     account, fills {{N}} from the agent's values, and has the
│                     no-Meta backstops (notify + send_read_receipt).
├── notification.py   ChannelWhatsAppNotification — overrides WhatsApp Notification. The
│                     same no-Meta redirect for automated / scheduled / event-driven sends.
├── templates.py      ChannelWhatsAppTemplates — neutralises every Meta-bound path on the
│                     template doctype (create blocked, edit allowed for the
│                     field mapping, update/delete/fetch are no-ops).
├── templates_sync.py `sync_templates` — asks each account's own adapter for its approved
│                     templates and mirrors them locally, one record per account.
│                     `scheduled_sync_all` is the 6-hourly scheduler entry.
├── routing.py        Picks the account for a lead (outbound) and for an inbound
│                     message. This is the multi-account brain (below).
├── webhook.py        The inbound guest endpoint: verify → kill-switch → drop
│                     non-CRM → enqueue → store. Maps delivery/read statuses to
│                     the ticks the CRM shows.
├── phone.py          Phone-number normalisation helpers / one-off sweep utility.
└── form_scripts/
    └── lead_whatsapp_template.js   The CRM Form Script (shipped as a fixture).
                      Intercepts the built-in "Send Template" button and opens our
                      account-scoped picker + variable-fill dialog.

tatva_connect/api/whatsapp.py        Backend for the picker: list_templates (scoped
                      to the lead's account), get_template_variables, get_field_options
                      (lead/profile fields to pick from), send_template_with_params.
                      Also refresh_messages_from_wati — the Refresh-button reconcile.

tatva_connect/tatva_connect/doctype/
├── wati_account_routing/   The routing rules doctype (Product Line / Group /
│                           Program → WhatsApp Account). Blocks duplicate rules.
└── wati_settings/          Single doctype: kill-switch + webhook secret.

tatva_connect/patches/add_whatsapp_message_id_unique_index.py
                      One-time DB patch: unique index on message_id so a redelivered
                      webhook can't create duplicate rows.
```

---

## Setting up MULTIPLE WATI accounts + routing rules

Each WATI tenant (e.g. Anaya, Goodflip) is its own WATI account with its own base
URL, its own templates, and its own WhatsApp number. In Frappe each maps to **one
`WhatsApp Account` record**. Here's the full setup for adding a second (or third…)
account without crossing wires.

### Step 1 — Create the WhatsApp Account (one per tenant)
Desk → **WhatsApp Account** → New:
- **URL**: the tenant base URL, e.g. `https://live-mt-server.wati.io/<tenant_id>`
- **Token**: the tenant's WATI JWT
- **Is WATI Account** (`custom_is_wati`): ✅ tick it
- **WATI Channel Number** (`custom_wati_channel_number`): that tenant's WhatsApp
  number (digits), shown as the outbound **From** number. Unique across accounts
  (enforced). *(Inbound is attributed by the per-account webhook token, not this field.)*
- **Webhook Token (WATI)** (`custom_webhook_token`): the per-tenant inbound secret —
  set a unique random value; it appears in this account's webhook URL (Step 4).

### Step 2 — Sync that account's templates
Desk → **WhatsApp Templates** list → **Sync** (or run
`tatva_connect.whatsapp.templates_sync.sync_templates` with no argument to sync **all**
active accounts at once). Templates are stored **per account** — the record name is
`templatename::Account Name`, so two tenants can both have e.g. `appointment_reminder`
without clobbering each other. The picker only ever shows the templates of the
account a given lead routes to.

### Step 3 — Add routing rules (who sends from which account)
Desk → **WATI Account Routing** → New. Each rule maps a slice of the lead taxonomy
to an account. A rule has up to three axes — **Product Line** (Vertical), **Group**,
**Program** — and a target **WhatsApp Account**. Leave an axis blank to mean "any".

A rule matches a lead **only if every axis it fills matches the lead**. Among all
matching rules, the **most specific wins**:

```
Program  (most specific)   beats   Group   beats   Product Line (least specific)
```

Examples:
| Rule | Means |
|---|---|
| Program = `Anaya Nivolumab` → Anaya account | leads in that program send via Anaya |
| Group = `Insurers` → Goodflip account | any lead in the Insurers group (with no more-specific rule) sends via Goodflip |
| Product Line = `Oncology` → Anaya account | a broad catch for all Oncology |
| (all three blank) → some account | an explicit catch-all (only if you choose to make one) |

**"Program = X **OR** Group = Y → same account"** is just **two separate rules**
both pointing at that account. They can't conflict.

### The guard-rails that keep it unambiguous
- **No global default.** If a lead matches **no** rule, the send is **blocked** with
  a clear error — we never silently fall back to some default tenant. (Configure a
  blank-axes catch-all rule if you actually want a default.)
- **No duplicate rules.** You can't save two rules with the identical
  (Product Line, Group, Program) combination — that's rejected on save.
- **No ambiguous tie.** If two equally-specific rules ever pointed at *different*
  accounts for one lead, the send raises instead of guessing.
- **Templates can't cross tenants.** The picker is scoped to the lead's routed
  account, so an agent can only pick a template that actually exists on the tenant
  the message will go out from. (No route → the picker says so and lists nothing.)

### Step 4 — Inbound: register the webhook per tenant
On **each** provider tenant, register the pretty, CHANNEL-uniform webhook URL. There is
deliberately no vendor segment — the trailing segment is **that account's own token**
(`WhatsApp Account → Webhook Token`), and the account names its own provider, so moving a
tenant between vendors is configuration rather than a URL an operator must re-register on a
dashboard we do not control:

```
https://<host>/webhooks/whatsapp/<token>
```
- nginx rewrites `/webhooks/whatsapp/<token>` → `?token=<token>` (see
  `nginx/frappe.conf.template`); the spine resolves the account from the token and the
  adapter from the account.
- The token does **double duty**: it authenticates the caller **and** identifies the
  receiving tenant — one secret per account, no `&account=` hint, no shared token, and
  no dependence on any WATI payload field (WATI inbound carries no reliable tenant id).
- Get the exact URL per account from the admin helper
  `tatva_connect.whatsapp.webhook.webhook_urls`.

If the account is known (from the token) but no lead routes to it for that number, the
message is still stored on the first lead by phone and the mismatch is logged for you to
fix (never mis-filed to the wrong account). Full setup: vault runbook
`02-operations/runbooks/09`.

---

## Day-to-day ops (quick reference)

| Task | Where |
|---|---|
| Turn WhatsApp on/off (send + receive) | Desk → **WATI Settings → Enabled** |
| Refresh the template list | **WhatsApp Templates** list → **Sync from WATI** (auto every 6h too) |
| Reconcile a lead's chat with WATI | the lead's **Refresh WhatsApp** button (header, leftmost) |
| Add a tenant | New **WhatsApp Account** (Step 1) + sync + routing rule + webhook |
| Change who sends from which account | **WATI Account Routing** rules |
| Map a template variable to a CRM field | open the template, set **Field Names** (optional; the picker also lets agents pick per-send) |
| See a lead's WhatsApp thread | the lead's **WhatsApp** tab (inbound + outbound + status ticks) |

## Notes / known limits
- **Templates with header/button (media, CTA) components**: the automated
  notification path sends the **body** params correctly; header/button params are
  not yet translated to WATI. Body-only templates are fully supported.
- The local `WhatsApp Templates` rows are a **read-only mirror** — edit templates on
  WATI, then re-sync. Editing the body locally won't push anywhere.
- **Triple error toasts** — a frappe-ui quirk (its `frappeRequest` invokes `onError`
  twice and re-throws), so one backend error shows ~3 toasts. Affects *all* of crm,
  not just WATI; cosmetic; not fixable without forking the frontend bundle.
- **Buttons** (Send Template hijack, Refresh WhatsApp) are shipped as **CRM Form
  Scripts** + a capture-phase DOM click interceptor — no crm fork. crm merges actions
  from all enabled form scripts, so WATI and Acefone scripts coexist.
