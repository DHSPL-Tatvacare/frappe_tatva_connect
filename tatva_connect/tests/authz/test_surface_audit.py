# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Cross-app permission-surface drift guard (TESTS.md §4, bucket F).

Generalizes lockdown.effective_all_guest_grants from {All, Guest} x {locked doctypes} to ANY
role across ALL doctypes, then diffs the LIVE grant surface against the blessed allowlist.json.
The allowlist is the APPROVED set; the test fails on anything BEYOND it — a new app, migration, or
fixture that WIDENS a role's reach (e.g. a future Helpdesk update granting Guest write to a CRM
doctype) lights up here. Removing a grant is NOT a failure (tightening is always safe).

Same idiom as the L4 drift guard in test_vapt_authz.py: collect every offending pair and fail ONCE
with the full list. effective_grants reads the perm TABLES directly (this is an audit of the perm
matrix itself, not a row-access test), resolving Custom DocPerm-overrides-stock exactly as Frappe
does — so frappe.get_all over DocPerm / Custom DocPerm is the correct tool here.
"""
import os

import frappe

from tatva_connect.tests.authz.base import AuthzTestCase

ALLOWLIST = os.path.join(os.path.dirname(__file__), "allowlist.json")


def effective_grants(role):
    """Every doctype `role` can EFFECTIVELY read/write/create/delete, resolved like Frappe does.

    When any Custom DocPerm exists for a doctype it OVERRIDES the stock DocPerm entirely (the stock
    rows still sit in `tabDocPerm` but are ignored), so we read Custom DocPerm when present, else
    DocPerm. Reading raw `tabDocPerm` alone is wrong — it reports a false grant for a doctype we've
    relocked via Custom DocPerm. Generalizes lockdown.effective_all_guest_grants to any role/doctype.

    AUDIT NOTE: this inspects the permission TABLES themselves (not whether a specific row is
    accessible), so frappe.get_all over DocPerm / Custom DocPerm is correct — a has_permission probe
    would answer a different (row-access) question. Returns [{role, doctype, r, w, c, d}] for rows
    with any positive grant at permlevel 0.
    """
    overridden = {p.parent for p in frappe.get_all("Custom DocPerm", fields=["parent"], distinct=True)}
    merged = {}
    for src, kind in (("DocPerm", "stock"), ("Custom DocPerm", "override")):
        rows = frappe.get_all(
            src,
            filters={"role": role, "permlevel": 0},
            fields=["parent", "read", "write", "create", "delete"],
        )
        for row in rows:
            doctype = row.parent
            use = "override" if doctype in overridden else "stock"
            if kind != use:
                continue
            if row.read or row.write or row.create or row.delete:
                acc = merged.setdefault(doctype, [0, 0, 0, 0])
                acc[0] |= int(row.read or 0)
                acc[1] |= int(row.write or 0)
                acc[2] |= int(row.create or 0)
                acc[3] |= int(row.delete or 0)
    return [
        {"role": role, "doctype": dt, "r": v[0], "w": v[1], "c": v[2], "d": v[3]}
        for dt, v in merged.items()
    ]


class TestSurfaceAudit(AuthzTestCase):
    def test_no_permission_surface_drift(self):
        with open(ALLOWLIST) as f:
            spec = frappe.parse_json(f.read())
        roles = spec["roles_audited"]
        approved = {(g["role"], g["doctype"]) for g in spec["grants"]}

        current = set()
        for role in roles:
            for g in effective_grants(role):
                current.add((g["role"], g["doctype"]))

        widened = sorted(current - approved)
        self.assertFalse(
            widened,
            "SURFACE DRIFT: {0} unapproved (role, doctype) grant(s) appeared beyond allowlist.json "
            "— a new app/migration/fixture widened the permission surface: {1}".format(
                len(widened), widened
            ),
        )
