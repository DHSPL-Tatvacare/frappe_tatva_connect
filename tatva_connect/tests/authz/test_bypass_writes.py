# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Every `ignore_permissions=True` site carries a tier marker, and the partner API never writes off-grain.
Partner tests call the inner `_upsert_one`/`_update_one`/`_delete_one`, since `@_api` swallows the raise."""
import ast
import os
import pathlib
import re

import frappe

from tatva_connect.api import partner
from tatva_connect.tests.authz import generator, grains, roster
from tatva_connect.tests.authz.base import AuthzTestCase, set_user
from tatva_connect.tests.authz.oracle import native_would_allow

# Tiers: a = system/seed/patch/worker, b = external input that self-gates first, c = pinned to session.user.
_TIERS = ("a", "b", "c")
_MARKER = re.compile(r"#\s*authz-ok:\s*tier-([abc])\b\s*[—-]?\s*(.*)", re.I)

_APP_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _bypass_sites():
    """Every real `ignore_permissions=True` call outside tests, as (relpath, lineno, func, tier, reason).
    Walks the AST, so a docstring or comment that only mentions the token is never counted."""
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
    """A pure source walk over bypass sites; needs no seeded data."""

    def test_every_ignore_permissions_write_carries_a_tier_marker(self):
        """A bypass with no tier marker is a hole nobody reviewed, and fails the build.
        The marker keys the review, so a line shift never breaks this lock."""
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
        """A tier-B marker names its gate, so a reviewer can check the gate is real."""
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

class TestBypassWrites(AuthzTestCase):
    """The partner's bypassing lead writes never reach a lead outside its grain.
    An allowed in-grain write never exceeds what the permission-checked path would grant."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()  # comms-off gate + class rollback floor
        generator.seed(commit=False)
        cls.partner_user = roster.email("partner")
        cls.in_grain = grains.GRAINS[0]      # Goodflip-Care / Anaya / Nivolumab — the partner's line
        cls.out_grain = grains.GRAINS[2]     # Tatvapractice / India / Field-Sales — a foreign line

    # ---- helpers -------------------------------------------------------------

    def _partner_caller_fields(self):
        """The (user, mp, is_sysmgr, parent_fields, child_allow) tuple the endpoints build, as the partner."""
        with set_user(self.partner_user):
            return partner._caller_fields()

    def _seed_lead(self, g, mobile, savepoint):
        """Insert a CRM Lead on grain `g` inside a named savepoint."""
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
        """A partner cannot plant a lead on a foreign line; forced routing stamps it back to their own grain.
        The create succeeds rather than raising, so the test checks where the lead landed."""
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
        """A partner cannot update or delete a foreign-line lead; the grain check refuses before the save."""
        _user, mp, is_sysmgr, parent_fields, child_allow = self._partner_caller_fields()
        # The gate raises DoesNotExistError today; the wider catch keeps a path change from passing falsely.
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

    def test_an_in_grain_create_lands_on_the_partners_line_through_the_api_only(self):
        """An in-grain partner create lands on the partner's own line.
        The role-less partner holds no native CRM Lead write, so the API is their only write door."""
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

            # Native must deny the partner create/write, so the API stays their only door to CRM Lead.
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
