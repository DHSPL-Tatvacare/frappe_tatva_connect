# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""No app, migration or fixture widens any audited role's grants beyond allowlist.json.
Removing a grant passes; only a widened (role, doctype) pair fails."""
import os

import frappe

from tatva_connect.tests.authz.base import AuthzTestCase

ALLOWLIST = os.path.join(os.path.dirname(__file__), "allowlist.json")


def effective_grants(role):
    """Every doctype `role` can read/write/create/delete at permlevel 0, as Frappe resolves it.
    Custom DocPerm replaces stock DocPerm for a doctype, so a relocked doctype shows no false grant."""
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
            f"SURFACE DRIFT: {len(widened)} unapproved (role, doctype) grant(s) appeared beyond allowlist.json "
            f"— a new app/migration/fixture widened the permission surface: {widened}",
        )
