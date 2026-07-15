# Partner API load test

Drives real LeadSquared data through the partner API at scale, on a local bench, and times every
call. It answers two questions the unit suites cannot: does the API hold up under real volume, and
does a real partner's data actually fit through it.

This is a **test harness, not a migration path.** It is not a second way to load production. The
migration loader (`docs/go-live/7-migrate-data/`) writes through the ORM as a trusted internal caller
and can stamp who owned a record and when it was made. This harness is an ordinary partner: it holds
no privileged handle, and the API refuses to be told those things. The two are read side by side, not
swapped for one another.

## Safety

- **LSQ is read-only.** `lsq.py` checks every endpoint against an allowlist before the request is
  made, so no create, update, capture or delete endpoint is reachable. Nothing is ever written back.
- **Credentials never enter the repo.** LSQ keys and partner tokens are read from the operator's
  gitignored creds directory. This repo is public.
- **Pulled data never enters the repo.** `data/` and `reports/` hold live patient records and are
  gitignored.
- **Nothing leaves the machine.** `preflight.py check` refuses the run unless every egress channel is
  dead. It never disarms one for you: an armed channel is the operator's call.
- **No CI, no deploy.** Everything here is a local script.

## Running it

Preflight first. It fails the run rather than repairing the site.

```bash
docker exec tatvalocal-backend-1 bash -lc \
  'cd /home/frappe/frappe-bench/sites && ../env/bin/python -B \
   /home/frappe/frappe-bench/apps/tatva_connect/tatva_connect/tests/live/load/preflight.py check'
```

Then pull, load, and replay:

```bash
.venv/bin/python -m tatva_connect.tests.live.load.pull anaya tatvapractice --leads 250
.venv/bin/python -m tatva_connect.tests.live.load.run  anaya tatvapractice
.venv/bin/python -m tatva_connect.tests.live.load.run  anaya tatvapractice --replay
```

The replay is the point, not a formality. Every write carries an `Idempotency-Key` derived from the
source record's own id, so a second pass must create nothing and replay the stored responses instead.
If the row counts move, retry safety is broken.

## Against a real deployment (UAT)

`uat.py` is the same core aimed somewhere else. It fuses the pull and the load into one command, takes
the target as a flag, and runs from the laptop — no bench, no container, no Frappe import. It is how the
API gets exercised from outside the network, as a partner actually reaches it.

```bash
# one terminal per grain; run them together and the concurrency is real
.venv/bin/python -m tatva_connect.tests.live.load.uat --grain anaya         --leads 50 --base https://<uat> --site <uat-host>
.venv/bin/python -m tatva_connect.tests.live.load.uat --grain tatvapractice --leads 50 --base https://<uat> --site <uat-host>
```

Tokens come from `.creds/partner-api-tokens.uat.json` (`--tokens` to point elsewhere); a deployment's
keys are its own and are never mixed with the bench's. A non-local target must be confirmed by typing
the host before anything is written.

Two things the local run has and this one does not, by construction: `preflight.py` cannot gate egress
or silence comms on a site it has no handle to, and `validate.py` cannot read the database to see what
truly landed — the API's own read-back is the only evidence available. **The run creates and does not
delete.** Every lead it made is written to `reports/<grain>.uat.load.json` under `created_leads`, which
is the only record of what to clean up.

## Why the pull is activity-first

Asking LSQ for leads and then their activities selects the wrong slice: most leads carry no activity
on a mapped event, so a lead-first slice loads almost nothing and tests almost nothing. The pull reads
the mapped event codes first, then resolves the leads those activities belong to, and takes the
richest. Every mapped code is swept, so no task type goes untested.

Activities are read through the bulk by-event endpoint. Only that endpoint returns the `mx_Custom_*`
slots the field map is written against; the per-lead endpoint returns an activity's envelope but not
its payload, and loading through it would create every activity blank.

## What it does not cover

The partner API has no endpoint for notes, payments or users, so those phases of a migration have no
equivalent here. It also cannot set `owner`, `creation` or `modified_by`, which are OUTPUT_ONLY: every
record lands owned by the partner user. Do not read a green run as a migration rehearsal.

## Files

| file | role |
|---|---|
| `preflight.py` | egress gate, toggle snapshot/restore. Runs in the container. |
| `lsq.py` | read-only LeadSquared client (allowlist-enforced) |
| `pull.py` | live LSQ -> `data/<account>/bundles.jsonl` |
| `shape.py` | LSQ record -> partner API request body |
| `client.py` | partner API client: envelope, idempotency, stopwatch |
| `run.py` | per-lead sequential load, latency percentiles, error taxonomy |
| `uat.py` | pull + load in one command against a real deployment, from the laptop |
