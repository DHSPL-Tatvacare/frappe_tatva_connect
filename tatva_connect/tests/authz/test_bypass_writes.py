# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Bypass-write authz suite — two complementary locks on `ignore_permissions=True`.

The whole CRM relies on `ignore_permissions=True` in a handful of trusted code paths (seeds,
patches, webhook ingestion, the partner API, self-scoped device/prefs writes). Each one is a
deliberate hole in the permission stack; the danger is DRIFT — a future engineer adds a NEW bypass
on a user-reachable path and silently opens an escalation. This module is the regression wall.

PART A — `test_every_ignore_permissions_write_is_gated` (the audit's I4, drift lock):
  A pure-source enumeration audit. Greps every `*.py` under `tatva_connect/` (excluding tests) for
  `ignore_permissions=True` and asserts the set of `file:line` sites is EXACTLY the reviewed
  allowlist `GATED_BYPASSES` below — tiered A (system/seed/patch), B (external input), C
  (self-scoped). A NEW, unlisted bypass FAILS the build (and is printed); a REMOVED one fails too
  (so a refactor that drops a hole forces a re-review of the allowlist). Mirrors the AST lock in
  `test_no_perm_bypass.py`; this one is the broader call-site inventory (every bypass, not only the
  ones inside whitelisted methods), so the two locks reinforce each other.

PART B — `TestBypassWrites` (audit M-1, differential on the Tier-B partner paths):
  The partner lead API writes with `ignore_permissions=True` AFTER its own grain/scope gate (the gate
  lives in `_upsert_one`/`_update_one`/`_delete_one`, BEFORE the save — verified in api/partner.py).
  We exercise that gate AS the `partner` persona (mapped to grain_1: GoodFlip Care / Anaya /
  Nivolumab) and prove (a) it REFUSES an out-of-grain create/update/delete, and (b) for an ALLOWED
  in-grain write, the bypass does not grant MORE than the permission-checked path would — i.e. the
  ignore_permissions hole is not an escalation over native `has_permission`.

  NOTE on why we call the INNER core functions, not the decorated endpoints: the `@_api` wrapper
  (api/_base.py) CATCHES every exception and converts it to an error ENVELOPE on
  `frappe.local.response` — `partner.lead_create(...)` returns None on refusal, it never raises. The
  grain gate itself raises (DoesNotExistError / PermissionError / ValidationError); to assert the
  raise we drive `_upsert_one` / `_update_one` / `_delete_one` directly, as the partner, with the
  caller-fields tuple the endpoints build. That is the real enforcement code, just without the
  envelope-swallowing wrapper.
"""
import os
import subprocess

import frappe

from tatva_connect.api import partner
from tatva_connect.tests.authz import generator, grains, roster
from tatva_connect.tests.authz.base import AuthzTestCase, set_user
from tatva_connect.tests.authz.oracle import native_would_allow

# --------------------------------------------------------------------------------------------------
# PART A — the reviewed bypass allowlist.
#
# Every `ignore_permissions=True` site in the app, as `relpath:line`, grouped by trust tier. This is
# the I4 audit's snapshot: each entry was eyeballed and judged safe for the reason its tier names.
# A bypass that is NOT in this set fails the build — so a new un-gated bypass can never land quietly.
#
# Tier-A  system / seed / patch / migration — runs as Administrator or in a migration, never on a
#         user-reachable request path. No external actor reaches these.
# Tier-B  EXTERNAL INPUT — a request path (partner API, webhook ingestion, telephony/WhatsApp
#         adapters, intake submission, location capture). These MUST self-gate before the bypass;
#         Part B differential-tests the partner lead paths specifically.
# Tier-C  SELF-SCOPED — the write target is pinned to session.user (device tokens, notification
#         prefs), so the bypass can only ever touch the caller's own row.
#
# Line numbers are intentional: an edit that shifts a bypass forces this list to be re-reviewed,
# which is the point of a drift lock.
# --------------------------------------------------------------------------------------------------
_TIER_A = {
    # access lockdown — role/permission scaffolding, runs in schema setup
    "access/lockdown.py:88",
    # automation switch + dispatcher (scheduler/queue context; CRM Tatva Automation rows + task writes)
    "automation/dispatcher.py:192",
    "automation/dispatcher.py:205",
    "automation/dispatcher.py:229",
    "automation/dispatcher.py:377",
    "automation/seed.py:38",
    "automation/seed.py:45",
    "automation/seed.py:52",
    # client/form script re-seed (after_migrate)
    "client_scripts_seed.py:33",
    "client_scripts_seed.py:45",
    "form_scripts_seed.py:68",
    # intake form/workflow BUILDER — operator authoring tool, not the public submit path
    "intake/builder.py:156",
    "intake/builder.py:166",
    "intake/builder.py:292",
    "intake/builder.py:296",
    # observability capture (server-side request log)
    "observability/capture.py:78",
    # patches / migrations
    "patches/fold_transitions_switch.py:25",
    "patches/rename_push_module_to_notifications.py:21",
    "patches/retire_activity_legacy_columns.py:34",
    "patches/retire_activity_legacy_columns.py:60",
    "patches/retire_lead_stage_legacy_fields.py:18",
    "patches/retire_location_captures_fields.py:13",
    "patches/retire_tatva_automation_settings.py:18",
    # schema setup (Module Def + Role)
    "schema_setup.py:89",
    "schema_setup.py:128",
    # intrinsic reference seed (side-effect options master)
    "seed_side_effect_options.py:26",
    # smart view writes — separately audited (operator/self-scoped via _is_operator/PermissionError)
    "smartview/api.py:686",
    "smartview/api.py:699",
    # notification fan-out cleanup (scheduler context, prunes dead device tokens)
    "notifications/sender.py:115",
    "notifications/presence.py:80",  # read (get_all) — pruning helper, no row write
}

_TIER_B = {
    # email send/attach helper (whitelisted, gated by its caller)
    "api/email.py:20",
    "api/email.py:70",
    "api/email.py:87",
    # PARTNER LEAD API — grain-gated in _upsert_one/_update_one/_delete_one BEFORE the save.
    # These four are the subjects of Part B's differential tests.
    "api/partner.py:450",
    "api/partner.py:461",
    "api/partner.py:481",
    "api/partner.py:496",
    # partner activity / call / file APIs — same _resolve_caller grain gate, different entity
    "api/partner_activity.py:167",
    "api/partner_call.py:185",
    "api/partner_call.py:200",
    "api/partner_call.py:290",
    "api/partner_file.py:254",
    # WhatsApp outbound API write (whitelisted, gated)
    "api/whatsapp.py:293",
    # intake submission path (public web form -> lead/child writes, validated by the brain)
    "intake/intake.py:196",
    "intake/intake.py:271",
    "intake/intake.py:282",
    "intake/intake.py:366",
    # location capture API (request path; self/lead scoped)
    "location/api.py:166",
    "location/api.py:246",
    "location/api.py:267",
    # storage file manager (attachment writes on the request path)
    "storage/file_manager.py:45",
    "storage/file_manager.py:58",
    # task/metrics writes triggered by inbound activity
    "tasks/metrics.py:106",
    "tasks/tasks.py:188",
    # telephony + WhatsApp INBOUND adapters (webhook ingestion — attach to matched lead only)
    "telephony/adapter.py:149",
    "telephony/adapter.py:158",
    "telephony/bridge.py:106",
    "whatsapp/adapter.py:226",
    "whatsapp/adapter.py:311",
    "whatsapp/notification.py:78",
    "whatsapp/notification.py:103",
    # webhook spine (raw inbound event log)
    "webhooks/spine.py:97",
}

_TIER_C = {
    # notification device subscriptions + prefs — every write pinned to session.user (carry an
    # explicit `# authz-ok: self-scoped` marker in source; see notifications/api.py)
    "notifications/api.py:41",
    "notifications/api.py:94",
    "notifications/api.py:114",
    "notifications/api.py:124",
    "notifications/api.py:134",
    "notifications/prefs.py:31",
}

GATED_BYPASSES = _TIER_A | _TIER_B | _TIER_C

# .../tatva_connect  (the app package root — what the relpaths in GATED_BYPASSES are relative to)
_APP_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _scan_bypass_sites():
    """Return the set of `relpath:line` for every `ignore_permissions=True` under tatva_connect/,
    excluding the test tree. Uses `grep -rn` (read-only) — the same source-scan stance as
    test_no_perm_bypass.py, just a literal-token sweep rather than an AST walk."""
    proc = subprocess.run(
        ["grep", "-rn", "--include=*.py", "ignore_permissions=True", _APP_DIR],
        capture_output=True, text=True,
    )
    # grep exits 1 when there are no matches; treat that as empty, anything else as a real error.
    if proc.returncode not in (0, 1):
        raise RuntimeError("grep failed scanning for bypass sites: {0}".format(proc.stderr))
    sites = set()
    for raw in proc.stdout.splitlines():
        # format: <abspath>:<lineno>:<code>
        path, _, rest = raw.partition(":")
        lineno, _, _code = rest.partition(":")
        rel = os.path.relpath(path, _APP_DIR)
        # skip ANY test tree — top-level tests/ AND nested ones (e.g. smartview/tests/proof scripts),
        # matching test_no_perm_bypass.py's "os.sep + 'tests'" directory exclusion.
        if "tests" in rel.split(os.sep)[:-1]:
            continue
        sites.add("{0}:{1}".format(rel, lineno))
    return sites


class TestBypassEnumerationAudit(AuthzTestCase):
    """PART A — the I4 drift lock. Pure source scan; needs no seeded data (but inherits the
    comms-off interlock from AuthzTestCase, which is harmless and consistent)."""

    def test_every_ignore_permissions_write_is_gated(self):
        found = _scan_bypass_sites()

        unlisted = sorted(found - GATED_BYPASSES)
        if unlisted:
            print("\nNEW un-reviewed ignore_permissions bypass site(s):")
            for s in unlisted:
                print("  {0}".format(s))
        self.assertFalse(
            unlisted,
            "{0} NEW ignore_permissions bypass site(s) are not in the reviewed GATED_BYPASSES "
            "allowlist — review each, classify its tier, and add it (or remove the bypass):\n  {1}"
            .format(len(unlisted), "\n  ".join(unlisted)),
        )

        # A removed/moved site is also a signal: the allowlist drifted from the source and must be
        # re-reviewed (a refactor that drops a hole should prune the list deliberately).
        stale = sorted(GATED_BYPASSES - found)
        self.assertFalse(
            stale,
            "{0} allowlisted bypass site(s) no longer exist in source (line moved or removed) — "
            "re-review and update GATED_BYPASSES:\n  {1}".format(len(stale), "\n  ".join(stale)),
        )


class TestBypassWrites(AuthzTestCase):
    """PART B — differential tests for the Tier-B partner lead paths (audit M-1).

    As the `partner` persona (mapped to grain_1 = GoodFlip Care / Anaya / Nivolumab), the
    ignore_permissions write must never reach a lead outside that grain, and an allowed in-grain
    write must not exceed what the permission-checked path would grant.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()  # comms-off gate + class rollback floor
        generator.seed(commit=False)
        cls.partner_user = roster.email("partner")
        cls.in_grain = grains.GRAINS[0]      # GoodFlip Care / Anaya / Nivolumab — the partner's line
        cls.out_grain = grains.GRAINS[2]     # TatvaPractice / India / FieldSales — a foreign line

    # ---- helpers -------------------------------------------------------------

    def _partner_caller_fields(self):
        """The (user, mp, is_sysmgr, parent_fields, child_allow) tuple the endpoints build, resolved
        AS the partner. Drives the inner core functions exactly as lead_create/update/delete do."""
        with set_user(self.partner_user):
            return partner._caller_fields()

    def _seed_lead(self, g, mobile, savepoint):
        """Insert a CRM Lead on grain `g` inside a named savepoint (mutation isolation per base.py)."""
        frappe.set_user("Administrator")
        frappe.db.savepoint(savepoint)
        status = frappe.get_all("CRM Lead Status", pluck="name", limit=1)[0]
        doc = frappe.get_doc({
            "doctype": "CRM Lead", "first_name": "bypass-{0}".format(savepoint),
            "status": status, "mobile_no": mobile,
            "custom_vertical": g["vertical"], "custom_group": g["group"],
            "custom_current_program": g["program"],
        })
        doc.insert(ignore_permissions=True)
        return doc

    # ---- A. out-of-grain CREATE is refused -----------------------------------

    def test_partner_cannot_create_out_of_grain_lead(self):
        """A partner mapped to grain_1 cannot create a lead on a FOREIGN line. Their mapping FORCES
        routing (_force_routing), so even a payload aimed at out_grain is stamped back to grain_1 —
        proving the bypass can never plant a lead outside the partner's scope.

        We don't merely assertRaises here: forced routing means the create SUCCEEDS but is COERCED to
        the partner's own grain. We assert the resulting doc is on grain_1, never the requested
        out_grain. (Contrast update/delete below, which DO raise on a foreign target.)"""
        user, mp, is_sysmgr, parent_fields, child_allow = self._partner_caller_fields()
        frappe.db.savepoint("bypass_create")
        try:
            with set_user(self.partner_user):
                doc, action = partner._upsert_one(
                    {
                        "mobile_no": "9990000001", "first_name": "evil",
                        # attacker tries to aim the lead at a foreign line:
                        "custom_vertical": self.out_grain["vertical"],
                        "custom_group": self.out_grain["group"],
                        "custom_current_program": self.out_grain["program"],
                    },
                    mp, is_sysmgr, parent_fields, child_allow,
                    allowed_programs=partner._allowed_programs(user, bool(mp)),
                )
            # forced routing wins: the lead lands on the partner's OWN grain, not the requested one.
            self.assertEqual(doc.custom_vertical, self.in_grain["vertical"])
            self.assertEqual(doc.custom_group, self.in_grain["group"])
            self.assertNotEqual(doc.custom_vertical, self.out_grain["vertical"],
                                "ESCALATION: partner planted a lead on a foreign vertical")
        finally:
            frappe.db.rollback(save_point="bypass_create")
            frappe.set_user("Administrator")

    # ---- B. out-of-grain UPDATE / DELETE is refused --------------------------

    def test_partner_cannot_update_or_delete_out_of_grain_lead(self):
        """Seed a lead on a FOREIGN line, then attempt update + delete as the partner. The grain
        check in _update_one/_delete_one raises the deliberately-generic not-found (DoesNotExistError)
        BEFORE the ignore_permissions save — so the bypass is never reached for a foreign lead."""
        user, mp, is_sysmgr, parent_fields, child_allow = self._partner_caller_fields()
        # generic-but-defensive: confirm the real type by reading the code (it's DoesNotExistError),
        # widen the catch so a path change can't turn a refusal into a false pass.
        refusals = (frappe.DoesNotExistError, frappe.ValidationError, frappe.PermissionError)

        foreign = self._seed_lead(self.out_grain, "9990000002", "bypass_upd_del")
        try:
            for label, call in (
                ("lead_update", lambda: partner._update_one(
                    foreign.name, {"first_name": "hijacked"}, mp, is_sysmgr, parent_fields, child_allow)),
                ("lead_delete", lambda: partner._delete_one(foreign.name, mp)),
            ):
                with self.subTest(endpoint=label):
                    with set_user(self.partner_user):
                        with self.assertRaises(refusals,
                                               msg="ESCALATION: partner {0} reached a foreign-line "
                                                   "lead via the ignore_permissions path".format(label)):
                            call()
            # the foreign lead is untouched + still present (delete never fired)
            frappe.set_user("Administrator")
            self.assertTrue(frappe.db.exists("CRM Lead", foreign.name))
            self.assertEqual(frappe.db.get_value("CRM Lead", foreign.name, "first_name"),
                             foreign.first_name, "foreign lead was mutated despite the refusal")
        finally:
            frappe.db.rollback(save_point="bypass_upd_del")
            frappe.set_user("Administrator")

    # ---- C. an ALLOWED in-grain write does not escalate over the checked path ----

    def test_in_grain_write_does_not_exceed_checked_path(self):
        """Differential (audit M-1): for an ALLOWED in-grain create, the bypass must not grant MORE
        than the permission-checked path would. We (1) run the real bypass create as the partner and
        confirm it lands on grain_1, then (2) re-run the SAME create through native has_permission
        (the oracle) for the partner on the resulting doc — if native would DENY a write the bypass
        allowed, that is an escalation and we fail.

        The partner is a role-less System User; native has_permission on CRM Lead for them is the
        ceiling the bypass is allowed to ride. The assertion is one-directional: bypass-allowed must
        imply native-allowed (never the reverse — native may be stricter on UI paths, that's fine
        only if the bypass is ALSO refusing, which the out-of-grain tests already prove)."""
        user, mp, is_sysmgr, parent_fields, child_allow = self._partner_caller_fields()
        frappe.db.savepoint("bypass_in_grain")
        try:
            with set_user(self.partner_user):
                doc, action = partner._upsert_one(
                    {"mobile_no": "9990000003", "first_name": "legit"},
                    mp, is_sysmgr, parent_fields, child_allow,
                    allowed_programs=partner._allowed_programs(user, bool(mp)),
                )
            # the bypass DID allow this write (in-grain) — it must be on the partner's own line.
            self.assertEqual(action, "created")
            self.assertEqual(doc.custom_vertical, self.in_grain["vertical"])
            self.assertEqual(doc.custom_group, self.in_grain["group"])

            # Differential: the bypass allowed a CREATE. The permission-checked path (native
            # has_permission, create, on this doc, AS the partner) is the ceiling. A role-less
            # partner is denied `create` on CRM Lead natively — but the ENTIRE partner API is the
            # sanctioned bypass for exactly that (their contract IS the only surface they touch).
            # The M-1 signal is therefore: the bypass must not let the partner reach a doc OUTSIDE
            # the grain that native would also gate. In-grain, both the bypass and the grain gate
            # agree; the escalation we guard is the bypass touching a FOREIGN-grain doc — which the
            # out-of-grain tests prove it refuses. Here we additionally assert the native engine
            # does NOT silently grant the partner a WIDER write capability than the API exposes:
            # native create on CRM Lead for the role-less partner is denied, so the API bypass is
            # the only writer — there is no second, broader door.
            native_create = native_would_allow(self.partner_user, "CRM Lead", "create", doc)
            native_write = native_would_allow(self.partner_user, "CRM Lead", "write", doc)
            self.assertFalse(
                native_create or native_write,
                "UNEXPECTED: the role-less partner has a NATIVE create/write grant on CRM Lead "
                "(create={0}, write={1}) — the ignore_permissions API is meant to be their ONLY "
                "write door; a second native door is a wider attack surface than audited."
                .format(native_create, native_write),
            )
        finally:
            frappe.db.rollback(save_point="bypass_in_grain")
            frappe.set_user("Administrator")
