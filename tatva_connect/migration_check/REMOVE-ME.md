# REMOVE ME — this whole folder is temporary

Built 2026-07-20 as an LSQ↔Frappe reconciliation demo so the operations team could spot-check
migrated leads at random during the LeadSquared migration. **It is not a product.** It has no tests
beyond a guard check, is not part of any invariant, and must not reach production.

Delete it once operations sign off on data parity. Ask them first — the sign-off is theirs.

## Teardown

```bash
git rm -r tatva_connect/migration_check
git rm -r tatva_connect/tests/migration_check
git rm -r tatva_connect/www          # this directory did NOT exist before the demo — it is all ours
```

Then delete the run artifacts on each site that used it:

```bash
rm -rf sites/<site>/private/files/migration_check
```

Then drop these keys from the UAT `site_config.json` and restart:

```
migration_check_enabled
migration_check_allowed_sites
migration_check_lsq_host
migration_check_lsq_access_key
migration_check_lsq_secret_key
migration_check_accounts      # per-grain LSQ keys and partner tokens
migration_check_base_url      # only set on containerised sites
migration_check_role          # optional, only if it was set
```

**No `bench migrate` is required.** There is deliberately no doctype, no fixture, no patch, no
`hooks.py` entry, no `modules.txt` line and no schema change — removal is a file delete.

## What it grew into

Three routes, all under the one folder:

| Route | What |
|---|---|
| `/migration_check` | one lead, LeadSquared vs Frappe, through the partner API |
| `/migration_check/totals` | whole-programme control totals — the page an owner signs |
| `/migration_check/batch` | up to 100 leads in one run, with a verdict each |

The two background jobs run on the `long` queue and write their results as files under
`private/files/migration_check/`. There is deliberately no doctype: a run needs state that survives
a restart, and files give that without leaving a table to unwind.

## Verify it is gone

```bash
curl -s -o /dev/null -w "%{http_code}\n" https://<site>/migration_check   # expect 404
grep -rn "migration_check" tatva_connect/                                  # expect no output
```

## Why it is safe until then

It is inert without config. `guard.py` refuses unless `migration_check_enabled` is truthy **and**
the current site is named in `migration_check_allowed_sites`. Production has neither key, so this
code returns 403 there even if it rides a merge. Login and a role check sit on top of that.

## Regenerating constants.py

`docs/` is gitignored and not in the container image, so `mapping.json` is unreadable at runtime and
the mapped LSQ event codes are baked into `constants.py`. If the mapping changes, regenerate:

```bash
cd docs/go-live/7-migrate-data/anaya
python3 -c "
import json; m = json.load(open('mapping.json'))
print('ACTIVITY_TASK_TYPES =', json.dumps(m['activity_task_types'], indent=1))
print('CALL_LOG_EVENTS =', json.dumps(m['call_log_events'], indent=1))
print('ACTIVITY_ATTACHMENT_SLOTS =', json.dumps(m['activity_attachments'], indent=1))
print('LEAD_CORE =', json.dumps(m['lead_core'], indent=1))
print('CHILDREN =', json.dumps({k: v['fields'] for k, v in m['children'].items()}, indent=1))
"
```

If you find yourself regenerating this more than once, the tool has outlived its purpose. Delete it.

## Related

- `docs/pending/2026-07-20-migration-check-temporary-teardown.md` — the open-item roster entry
- `docs/plans/2026-07-20-migration-check-reconciliation-page.md` — the plan it was built from
