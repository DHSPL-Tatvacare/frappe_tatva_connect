# tatva_frappe_custom

TatvaCare's in-house customization layer for our Frappe CRM. It holds the custom app
**`tatva_connect`** plus the API documentation portal (`api-docs/`).

This is **not a general-purpose Frappe app.** Every doctype, field, override, and automation
here encodes TatvaCare's Patient Support Program processes, integrations, and data model.
It is built to run against *our* CRM, *our* WhatsApp/telephony accounts, and *our* routing
taxonomy — nothing here is meant to be reused elsewhere.

---

## What `tatva_connect` adds on top of the framework

**Custom doctypes (TatvaCare data model)**
- Lead child-profile tables — Plan, Lab, Care Providers, Acquisition, Drug Program.
- Routing taxonomy — Vertical, Group, Program; plus Lead Stage, City, Doctor, Hospital, Task Type.
- Partner API config — Lead API Field, Lead API Mapping (+ child).
- Integration config — WATI Settings/Account Routing, Acefone Settings/Account/Routing, Automation Settings.
- Intake + task-checklist doctypes for web enrolment and follow-up.

**Overrides (no forking — all via `hooks.py`)**
- WhatsApp Message / Notification / Templates → routed through **WATI** (never Meta).
- Click-to-call + call-log → **Acefone**, riding the native CRM telephony UI.

**Partner Lead API** (`tatva_connect/api/partner.py`)
- A gated, per-partner create/get/update/delete/bulk surface over CRM Lead, with
  per-line dedup, child-table upsert-by-key, anti-enumeration, rate limiting, and a
  uniform error contract.

**Automations & schema-as-code**
- Lead validate chain (phone/routing canonicalization, dedup, stage validation, headline-metric sync).
- Inbound WhatsApp handling, web-intake → routed lead, follow-up task creation.
- Custom fields, property setters, layouts, and master data ship as **fixtures**; structural
  changes ship as **patches** — so `bench migrate` reproduces the full schema.

**API docs portal** (`api-docs/`)
- The Zudoku site served at `https://one.tatvacare.in/docs`. Build/ship with `cd api-docs && ./deploy-docs.sh`.

---

## Layout

| Path | What |
|---|---|
| `tatva_connect/` | The Frappe app — hooks, doctypes, api, automation, wati/, acefone/, fixtures, patches. |
| `api-docs/` | The documentation portal (buildable static site). |
| `nginx/` | Frontend nginx template carrying the durable `/docs` route. |

Strategy, architecture, and runbooks live in the internal Obsidian vault, not in this repo.

---

> **Footnote — please do not clone.** This is heavy, TatvaCare-specific customization wired to
> our environment, accounts, and data. It is published for our own deployment workflow, not for
> reuse. If you clone or run it elsewhere, you do so entirely at your own risk — it is unsupported
> and will not behave sensibly outside TatvaCare's setup.
