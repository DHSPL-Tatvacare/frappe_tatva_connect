<div align="center">

<img src=".github/assets/tatva-logo.png" width="88" height="88" alt="TatvaCare" />

# frappe_tatva_connect

**The backend for TatvaCare CRM — doctypes, APIs, integrations, and all business logic. Part of the TatvaCare One platform.**

[![Frappe](https://img.shields.io/badge/Frappe-v16-0089FF?logo=frappe&logoColor=white)](https://frappeframework.com)
[![Python](https://img.shields.io/badge/Python-3.10+-3776AB?logo=python&logoColor=white)](https://www.python.org)
[![MariaDB](https://img.shields.io/badge/MariaDB-003545?logo=mariadb&logoColor=white)](https://mariadb.org)
[![Redis](https://img.shields.io/badge/Redis-DC382D?logo=redis&logoColor=white)](https://redis.io)
[![Docker](https://img.shields.io/badge/Docker-2496ED?logo=docker&logoColor=white)](https://www.docker.com)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](./license.txt)

</div>

## Overview

`frappe_tatva_connect` is the **backend** of TatvaCare CRM — part of **TatvaCare One**, the single
platform our teams use across sales and operations. It holds the custom Frappe app **`tatva_connect`**
(TatvaCare's data model, integrations, and business rules) and the partner **API documentation portal**
(`api-docs/`). It is built for *our* environment and accounts — not a general-purpose Frappe app.

## Architecture — two repos, one product

| Repo | Role |
|---|---|
| **`frappe_tatva_connect`** (this) | the **backend** — doctypes, APIs, integrations, and **all business logic** |
| **[`frappe_tatva_crm`](https://github.com/DHSPL-Tatvacare/frappe_tatva_crm)** | the **frontend** — the single-page app teams use |

Every rule — permissions, grain scoping, lead and messaging routing, automations, intake — lives here and
is enforced **server-side, fail-closed**. The two repos ship together on one site and move in lockstep.

## Built on Frappe

[Frappe](https://frappeframework.com) is an open-source low-code framework (Python backend, Vue/JS
frontend, an ORM where every table is a **DocType**, a built-in permission engine, REST API, and
background jobs). We run a Frappe **v16** bench and customize it through `tatva_connect` — we never fork
Frappe core. Alongside CRM (forked, frontend-only) we run Helpdesk, LMS, Insights, Wiki, Payments,
Telephony, and a WhatsApp app — all near-stock.

## What `tatva_connect` adds

All TatvaCare-specific behaviour lives here, registered through `hooks.py` with no fork of Frappe core:
the business doctypes (routing taxonomy and the lead/care data model), the messaging and telephony
integrations, a partner API, an automation engine, smart views, file storage, observability, and the
access hardening that enforces the Frappe permission layer end to end. Everything is grain-scoped
(`vertical::group::program`) and ships dormant. Full inventory in [`docs/INVENTORY.md`](./docs/INVENTORY.md).

## Tech stack

Frappe v16 · Python 3.10+ · MariaDB · Redis · Vue 3 / frappe-ui (frontend) · Docker (deploy) ·
object storage for files · WhatsApp and telephony integrations · push notifications.

## Deploy (two lanes)

1. **Automated:** build one image from `apps.json` → install apps → `bench migrate` (applies all
   doctypes, fixtures, patches, and the `after_migrate` orchestration) → `enable-scheduler`.
2. **Manual (operator):** run `docs/go-live/3-seed/db-seeds` → publish the handbook → fill Settings forms and flip the
   automation toggles → run the data migration.

Everything ships **dormant** — nothing fires until an operator switches it on. Full posture in
[`CICD.md`](./CICD.md); step-by-step in [`docs/prod-deploy/DEPLOY.md`](./docs/prod-deploy/DEPLOY.md).

## Documentation

| Doc | For |
|---|---|
| [`CLAUDE.md`](./CLAUDE.md) / [`AGENTS.md`](./AGENTS.md) | Working rules — the invariants every change must follow |
| [`CICD.md`](./CICD.md) | Deploy posture: the two lanes, what is automated vs manual, and why |
| [`docs/INVENTORY.md`](./docs/INVENTORY.md) | Every custom doctype, override, hook, and endpoint |
| [`docs/prod-deploy/DEPLOY.md`](./docs/prod-deploy/DEPLOY.md) | Step-by-step production runbook |
| `api-docs/` | Partner API reference (Zudoku), served at `/docs` |

## License

[GNU AGPL-3.0-or-later](./license.txt) © TatvaCare. `tatva_connect` is a combined work with Frappe CRM
(AGPL-3.0); the deployed whole is governed by the AGPL, including its network-use (source-offer) clause.

---

> **Disclaimer.** This repository is specific to TatvaCare's environment, accounts, and data model.
> Fork or run it **at your own risk** — it is business-specific and unsupported outside TatvaCare's setup.
> This repository is the source of truth: make changes here, then commit and sync to the remote — the
> bench is a disposable copy.
