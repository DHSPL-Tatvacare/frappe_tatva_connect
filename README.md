<div align="center">

<img src=".github/assets/tatva-logo.png" width="88" height="88" alt="TatvaCare" />

# frappe_tatva_connect

**TatvaCare's healthcare CRM backend: the `tatva_connect` Frappe app that holds our data model, integrations, and business logic.**

[![Frappe](https://img.shields.io/badge/Frappe-v16-0089FF?logo=frappe&logoColor=white)](https://frappeframework.com)
[![Python](https://img.shields.io/badge/Python-3.10+-3776AB?logo=python&logoColor=white)](https://www.python.org)
[![MariaDB](https://img.shields.io/badge/MariaDB-003545?logo=mariadb&logoColor=white)](https://mariadb.org)
[![Redis](https://img.shields.io/badge/Redis-DC382D?logo=redis&logoColor=white)](https://redis.io)
[![Docker](https://img.shields.io/badge/Docker-2496ED?logo=docker&logoColor=white)](https://www.docker.com)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](./license.txt)

</div>

## Overview

`frappe_tatva_connect` is the backend of TatvaCare's healthcare CRM. It holds the custom Frappe app
`tatva_connect` (TatvaCare's data model, integrations, and business rules) on top of a Frappe v16
bench. It is built for our environment and accounts, not a general-purpose Frappe app.

## Two repos, one product

| Repo | Role |
|---|---|
| `frappe_tatva_connect` (this) | backend: doctypes, APIs, integrations, and all business logic |
| [`frappe_tatva_crm`](https://github.com/DHSPL-Tatvacare/frappe_tatva_crm) | frontend: the single-page app teams use |

Every rule (permissions, grain scoping, lead and messaging routing, automations, intake) is enforced
server-side and fail-closed. The two repos ship together on one site and move in lockstep.

## Built on Frappe

[Frappe](https://frappeframework.com) is an open-source low-code framework: a Python backend, an ORM
where every table is a DocType, a permission engine, a REST API, and background jobs. We run Frappe
v16 and customize it only through `tatva_connect`, registered in `hooks.py`; we never fork Frappe
core. Alongside CRM (forked, frontend-only) we run Helpdesk, LMS, Insights, Wiki, Payments,
Telephony, and a WhatsApp app, all near-stock.

## What `tatva_connect` adds

TatvaCare-specific behaviour, all grain-scoped (`vertical::group::program`) and shipped dormant: the
business doctypes (routing taxonomy and the lead and care data model), WhatsApp (WATI) and telephony
(Acefone) integrations, a partner API, an automation engine, smart views, object storage for files,
observability, and the access hardening that enforces the Frappe permission layer end to end.

## Deploy

1. **Automated:** build one image from the per-environment apps file, install the apps, run
   `bench migrate` (which applies doctypes, fixtures, patches, and the after-migrate orchestration),
   then enable the scheduler.
2. **Operator:** run the seed bundles, fill the Settings forms, flip the automation toggles, and run
   the data migration.

Everything ships dormant: nothing fires until an operator switches it on.

## License

[GNU AGPL-3.0-or-later](./license.txt), TatvaCare. `tatva_connect` is a combined work with Frappe CRM
(AGPL-3.0); the deployed whole is governed by the AGPL, including its network-use (source-offer)
clause.

---

> This repository is specific to TatvaCare's environment, accounts, and data model. Fork or run it at
> your own risk; it is business-specific and unsupported outside TatvaCare's setup. This repository is
> the source of truth: change it here, then commit and sync. The bench is a disposable copy.
