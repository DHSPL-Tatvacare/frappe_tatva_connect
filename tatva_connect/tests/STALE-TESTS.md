# Stale tests — index and status

**What this is.** During the 2026-07-23 grain-rename + catalog-consolidation work, a full-suite run went
broadly red. On investigation almost none of it was product breakage — it was **test scaffolding that had
not kept up with the one-brain refactor (18–21 July)** plus one config state on the dev bench. This file
is the index of what was stale, what was fixed, and the few real findings left open.

**Read this first — the two traps that inflate the failure count:**

1. **Full-suite pollution.** Many "failures" are cascade: one test's setUp/teardown leaves DB or cache
   state that breaks the next module. Run a module **in isolation** before believing it is broken.
   Example: `test_section_history` showed ~7 errors in the full run; run alone it had exactly **one**
   real failure.
2. **The armed registry flag.** `Access::Grain::registry` is **on** on the dev bench. Many grain tests
   were written for the flag **off** (Assignment-Rule entitlement) and break when it is armed (native
   User-Permission entitlement). The fix is to make the test **pin the flag** it needs, not to change any
   product code.

Product code was correct in every case below. Fixes are test-file-only.

---

## Index

| Test | Bucket | Root cause | Status |
|---|---|---|---|
| `access/test_resolve_fields_gates_a_real_user` | armed flag | assumed flag OFF; armed bench reads User Permission | **fixed** — pins flag OFF (its fixtures are Assignment-Rule based) |
| `access/test_a_view_is_scoped_to_one_grain` | armed flag | `grain_entitled` registry-clamp rejects synthetic grains when armed | **fixed** — mocks `_registry_enabled` (already mocks `entitled_grains`) |
| `access/test_every_emittable_grain_has_a_contract` | stale assertion | counted the Facebook mapping (no `partner_user`) as emittable | **fixed** — mirrors `_partner_grain`'s real filter |
| `lead/test_section_history` | narrow fixture | fixture contract ticked only the multi-row field, making every parent field non-universal | **fixed** — also ticks `lead:status`, the parent field it asserts on |
| `authz/test_surface_audit` | drift guard fired | 10 new read-only grants appeared past the 07-13 allowlist baseline | **fixed** — `allowlist.json` re-blessed (all verified read-only, no PII) |
| `lead/test_three_doors_one_shape` | stale fixture + real finding | setUpClass built an intake form with no internal contract, and ran `sync_form` before the intake switch was armed | **partially fixed** — see below |
| `workflow_engine/tests/*`, task-type / automation tests | parked | the workflow/automation layer is under active rework | **not touched** (out of scope by instruction) |

---

## Detail on the fixes

**armed-flag cluster.** A test that depends on `Access::Grain::registry` must set it deterministically,
not inherit the bench's ambient state. Where the fixtures are Assignment-Rule based, pin the flag OFF and
restore it (`test_resolve_fields_gates_a_real_user`). Where entitlement is already mocked, mock
`_registry_enabled` too (`test_a_view_is_scoped_to_one_grain`).

**`test_every_emittable_grain_has_a_contract`.** `entitlement._partner_grain` resolves a partner mapping
by `{partner_user, enabled}`. A mapping with no `partner_user` (the Facebook ingestion contract) is handed
to nobody, so it is not "emittable". The test now filters the same way.

**`test_section_history`.** The panel shows a field only where the lead's grain is covered by a contract
ticking it OR the field is universal (`lead/detail.py::_select`). A test contract that ticks a single
field makes every *other* field non-universal — so the parent-field case needs its subject (`lead:status`)
granted explicitly, exactly as the multi-row field is.

**`test_surface_audit`.** A cross-app permission drift guard. Ten `(role, doctype)` pairs appeared beyond
the blessed set as new doctypes landed (CRM Grain; CRM Bulk Job + Result; six CRM Workflow* engine
doctypes). All are `r=1, w=c=d=0` — read-only — and none holds patient/PII data, so they were re-blessed
into `allowlist.json`. The workflow ones will need a re-bless when the workflow rework lands.

---

## Open findings — NOT masked, need a decision

`test_three_doors_one_shape` is "the test that matters" (CLAUDE.md: three leads must be indistinguishable
but for `source` and `custom_source_origin`). Its setUp was broken by two stale-fixture bugs, both fixed:
it now builds an internal contract for the fixture grain, and arms the intake switch before `sync_form`.
With setUp working, the test **runs** and two of four sub-tests pass (`same lead`, `same grain`). The other
two surface real differences that must be reviewed rather than silenced:

1. **`test_each_door_still_records_which_door_it_was`** — the fixture's partner and Facebook contracts set
   no `source`, so both leads land `source = None` and only 2 of 3 doors are distinguishable. Production
   stamps a distinct `source` per door (`partner.py:523`); the fixture is simply incomplete. A faithful
   fixture would set three distinct sources — deferred here so it is reviewed, not rubber-stamped.

2. **`test_no_door_invents_a_child_table_the_others_do_not`** — the Facebook door fills
   `custom_acquisition_profile` (an `acq:touch_at` row derived from the payload's `created_time`,
   `lead_sync/source.py:95`). That is a **third** way the doors differ, beyond the two the constitution
   permits. Either the invariant should be amended to allow the Facebook acquisition touch as legitimate
   door provenance (like `source`), or the fold should not record it — a **product/architecture decision**,
   not a test edit. Left red on purpose.

---

## How to re-run

```
# one module, isolated (the honest signal):
bench --site <site> run-tests --module tatva_connect.tests.lead.test_section_history

# the access cluster:
bench --site <site> run-tests --module tatva_connect.tests.access.test_resolve_fields_gates_a_real_user
```

Run one thing at a time against a bench (lock-wait/deadlock otherwise), and pause the queue workers +
scheduler for suites that touch `tabUser` (access-03 style), then restart them.
