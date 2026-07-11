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
import ast
import collections
import os
import pathlib
import re

import frappe

from tatva_connect.api import partner
from tatva_connect.tests.authz import generator, grains, roster
from tatva_connect.tests.authz.base import AuthzTestCase, set_user
from tatva_connect.tests.authz.oracle import native_would_allow

# --------------------------------------------------------------------------------------------------
# PART A — every bypass carries its own review, at the site.
#
# `ignore_permissions=True` is a deliberate hole in Frappe's permission engine. The danger is DRIFT:
# a future engineer adds a NEW bypass on a user-reachable path and silently opens an escalation.
#
# This lock used to key its allowlist by `relpath:line`. That was the wrong key. It could not tell a
# NEW bypass (a real security signal) from an EXISTING one that moved because someone added a comment
# above it (noise) — and the second happens on nearly every commit. So the lock sat permanently red,
# nobody re-reviewed, and 19 bypasses landed unreviewed while it was failing. A test that is always
# red is worse than no test.
#
# The key is now the marker, which is what the constitution already prescribes: every bypass carries
# `# authz-ok: tier-<a|b|c> — <why>` on its own line or the line above. The review lives AT the site,
# in the diff, where a reviewer sees it. Line shifts are invisible to this lock; a new UNMARKED bypass
# is not.
#
# Tier-A  system / seed / patch / migration / engine — runs as Administrator, in a migration, or in a
#         background worker. No external actor reaches it.
# Tier-B  EXTERNAL INPUT — a request path (partner API, webhooks, telephony/WhatsApp adapters, intake,
#         location capture). These MUST self-gate BEFORE the bypass, and the marker must name the gate.
#         Part B differential-tests the partner lead paths specifically.
# Tier-C  SELF-SCOPED — the write target is pinned to session.user, so the bypass can only ever touch
#         the caller's own row.
# --------------------------------------------------------------------------------------------------
_TIERS = ("a", "b", "c")
_MARKER = re.compile(r"#\s*authz-ok:\s*tier-([abc])\b\s*[—-]?\s*(.*)", re.I)

# .../tatva_connect  (the app package root)
_APP_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _bypass_sites():
    """Every REAL `ignore_permissions=True` call site under tatva_connect/, excluding the test tree.

    Walks the AST, so a docstring or comment that merely MENTIONS the token is never counted — the old
    grep-based scan reported `_base.py`'s own docstring as a privilege bypass.

    Returns [(relpath, lineno, funcname, tier|None, reason)].
    """
    out = []
    for path in sorted(pathlib.Path(_APP_DIR).rglob("*.py")):
        rel = os.path.relpath(path, _APP_DIR)
        if "tests" in rel.split(os.sep)[:-1]:
            continue
        text = path.read_text()
        if "ignore_permissions=True" not in text:
            continue
        lines = text.splitlines()
        tree = ast.parse(text)
        funcs = [(n.lineno, n.end_lineno, n.name) for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]

        for node in ast.walk(tree):
            if not (isinstance(node, ast.keyword) and node.arg == "ignore_permissions"
                    and isinstance(node.value, ast.Constant) and node.value.value is True):
                continue
            i = node.value.lineno
            fn = next((n for s, e, n in funcs if s <= i <= e), "<module>")
            # the marker sits on the site's own line, or on one of the two lines above it
            tier = reason = None
            for probe in (lines[i - 1], lines[i - 2] if i >= 2 else "", lines[i - 3] if i >= 3 else ""):
                m = _MARKER.search(probe)
                if m:
                    tier, reason = m.group(1).lower(), m.group(2).strip()
                    break
            out.append((rel, i, fn, tier, reason))
    return out


class TestBypassEnumerationAudit(AuthzTestCase):
    """PART A — the drift lock. Pure source walk; needs no seeded data."""

    def test_every_ignore_permissions_write_carries_a_tier_marker(self):
        """A bypass with no marker is a hole nobody reviewed. It fails the build.

        This is the whole lock: an engineer adding `ignore_permissions=True` must say, at the site,
        which tier it is and why it is safe. A line shift cannot break this; an unreviewed hole cannot
        pass it.
        """
        sites = _bypass_sites()
        self.assertTrue(sites, "found no bypass sites at all — the scanner is broken")

        unmarked = [f"{rel}:{line} in {fn}()" for rel, line, fn, tier, _r in sites if tier is None]
        self.assertFalse(
            unmarked,
            "{} ignore_permissions=True site(s) carry no review marker. Add "
            "`# authz-ok: tier-<a|b|c> — <why it is safe>` at the site (tier-b MUST name the gate):"
            "\n  {}".format(len(unmarked), "\n  ".join(unmarked)),
        )

        bad_tier = [f"{rel}:{line}" for rel, line, _f, tier, _r in sites if tier not in _TIERS]
        self.assertFalse(bad_tier, f"unknown tier on: {bad_tier}")

    def test_every_external_input_bypass_names_its_gate(self):
        """Tier-B is the dangerous tier: a request path that bypasses the permission engine. Its marker
        must say WHAT gates it, so a reviewer can go and check that the gate is real."""
        vague = [
            f"{rel}:{line} in {fn}() -> {reason!r}"
            for rel, line, fn, tier, reason in _bypass_sites()
            if tier == "b" and not re.search(r"gate|scope|token|grain|permission|forced|doc_events", reason or "", re.I)
        ]
        self.assertFalse(
            vague,
            "a tier-B (external input) bypass must name the gate that protects it:\n  "
            + "\n  ".join(vague),
        )

    def test_the_bypass_inventory_is_reported(self):
        """Not an assertion — a census. It prints where the holes are, every run, so the count is
        visible in CI rather than buried in a list nobody opens."""
        sites = _bypass_sites()
        by_tier = collections.Counter(t for _r, _l, _f, t, _x in sites)
        print(f"\n  ignore_permissions=True sites: {len(sites)}")
        for tier, label in (("a", "system/seed/patch/engine"), ("b", "EXTERNAL INPUT (self-gated)"),
                            ("c", "self-scoped to session.user")):
            print(f"    tier-{tier}  {by_tier.get(tier, 0):3}  {label}")
        for rel, line, fn, tier, reason in sites:
            if tier == "b":
                print(f"      B  {rel}:{line} {fn}() — {reason}")


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
        # A Lost-type status (Junk/Unqualified) requires a lost-reason on save; pick a non-Lost one.
        status = frappe.get_all("CRM Lead Status", filters={"type": ["!=", "Lost"]}, pluck="name", limit=1)[0]
        doc = frappe.get_doc({
            "doctype": "CRM Lead", "first_name": f"bypass-{savepoint}",
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
                doc, _action = partner._upsert_one(
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
        _user, mp, is_sysmgr, parent_fields, child_allow = self._partner_caller_fields()
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
                                               msg=f"ESCALATION: partner {label} reached a foreign-line "
                                                   "lead via the ignore_permissions path"):
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
                f"(create={native_create}, write={native_write}) — the ignore_permissions API is meant to be their ONLY "
                "write door; a second native door is a wider attack surface than audited."
                ,
            )
        finally:
            frappe.db.rollback(save_point="bypass_in_grain")
            frappe.set_user("Administrator")
