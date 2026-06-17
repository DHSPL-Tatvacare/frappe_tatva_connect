# Observability — Partner API & Inbound Webhook analytics

A two-tier, Prometheus-style metrics plane for our external-facing endpoints, built on
plain Frappe doctypes so the data is queryable in Desk and chartable in Frappe Insights.

```
 every request ─► CRM API Request Log ──(rollup job, every 6h)──► CRM API Metric
                  raw, ~7 days (UI-purged)    additive merge       5min·hour·day, immortal
```

## The doctypes (module: Observability)

| DocType | Role | Lifetime |
|---|---|---|
| `CRM API Request Log` | One row per partner-API / inbound-webhook hit | **~7 days** (Log Settings — see below) |
| `CRM API Metric` | Pre-aggregated buckets `(bucket_start, granularity, channel, endpoint, source)`; only **additive** primitives (counts, sum/min/max latency, latency-band histogram) | 5-min rows **90 days**, hour/day rows **forever** |
| `CRM API Metric Settings` | Single: rollup watermark, lag, 5-min retention, last-run log | — |

Percentiles and rates are **not stored** — they are computed at read time from the
histogram bands (p95 = interpolate across cumulative bands), exactly like Prometheus
`histogram_quantile`. That is why every stored column is additive and the rollup is just
`SUM`/`MIN`/`MAX`.

## What gets logged

Only the ~10 partner-API methods and the inbound webhooks — never the other Desk/CRM
endpoints. The allow-list is `_WATCH` in `capture.py`, matched with the same anchored
`startswith` the partner-API error handler uses. The prefixes are not hand-typed — they
reuse `api.partner._PARTNER_PATH` and are derived from the webhook handler modules
(`_method_prefix(module)`), so a module rename can't silently stop logging:

- `_PARTNER_PATH` → channel **Partner API** (source = the authenticated caller)
- `whatsapp.webhook` module → channel **Inbound Webhook** (source WhatsApp)
- `telephony.handler` module → channel **Inbound Webhook** (source `TELEPHONY_MEDIUM`)

To log a new external endpoint, add a `(prefix, channel, source)` row to `_WATCH` — the
prefix being a reused constant or `_method_prefix(<that module>)`, never a literal.

## The 7-day raw retention is UI config — NOT shipped as code

Retention policy is operator config, so (per our "no config in code" rule) it is **not**
declared via the `default_log_clearing_doctypes` hook. The raw table grows until you set
this **once per site** after the first deploy. Two equivalent ways:

**UI (preferred):** Desk → search **Log Settings** → *Logs to Clear* → add a row:
`Document Type = CRM API Request Log`, `Days = 7` → Save. Frappe's daily cleanup job
then trims it automatically.

**Console (same effect, scriptable):**
```python
# bench --site <site> console
import frappe
ls = frappe.get_doc("Log Settings")
ls.append("logs_to_clear", {"ref_doctype": "CRM API Request Log", "days": 7})
ls.save()
frappe.db.commit()
```

> Until this is set, `CRM API Request Log` is never purged. The aggregate `CRM API Metric`
> is unaffected either way — the rollup runs every 6h and reads raw rows long before 7 days.

## The rollup job

`tatva_connect.observability.rollup.run`, scheduled at `0 */6 * * *` (alongside WATI sync).
Each run, under a `GET_LOCK` advisory lock:

1. raw → 5-min buckets (closed buckets only, floored by `lag_seconds`)
2. 5-min → hourly, hourly → daily (additive re-aggregation)
3. delete 5-min rows older than `fine_grain_retention_days` (default 90)
4. advance the watermark; write a one-line `last_run_log`

It is idempotent (upsert keyed on `name = SHA1(grain)`), so re-running is safe and the
lock only prevents two workers duplicating work.

**Backfill:** clear `last_rolled_until` in *CRM API Metric Settings* and run
`bench --site <site> execute tatva_connect.observability.rollup.run` (bounded by the
~7-day raw window).

## Deploy

`bench migrate` does everything structural:
- `register_observability_module` (pre-sync) creates the module on existing sites
- the three doctypes sync (tables created)
- `add_observability_indexes` (post-sync + after_migrate) builds the composite indexes

No `bench build` needed (no frontend). After deploy, set the Log Settings retention (above).

## Charts (next phase — not built yet)

`CRM API Metric` is the single source for the dashboard. When we add the workspace tiles,
follow the Workspace / Workspace Sidebar **fixture** runbook (hand-curated
`workspace.json` + `workspace_sidebar.json`, filtered by name, synced on migrate — never
`export-fixtures`). Number Cards / Dashboard Charts read `CRM API Metric` filtered by
`granularity` (e.g. `hour`), with error-rate = `error_count/request_count` and percentiles
derived from the `b*` histogram columns.
