# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Retire `CRM Facebook Settings`. Its whole purpose was to hold THE one Meta app, and there is no one app.

A Facebook token is issued by exactly one app, and the business runs six across two Business Managers, so
the credential is a row per app (`CRM Facebook App`) and every record holding a token names its own. What
remained on the Single once the credential moved — the Graph version and the fetch page size — belongs to
the app as well: one app can then be moved to a newer Graph version and watched while the others stay.

Nothing is carried forward. The app id and secret are re-entered per app, deliberately: there is no
honest way to decide which of six apps an inherited pair belonged to, and guessing wrong is the exact
silent failure this work exists to end.

A Single keeps no table of its own — its values live in `tabSingles` and its Password field in `__Auth` —
so deleting the DocType leaves both behind unless they are named. End state declared, nothing assumed
about what ran before; idempotent on a site that never had it.
"""
import frappe

SETTINGS = "CRM Facebook Settings"


def execute():
	frappe.db.delete("Singles", {"doctype": SETTINGS})
	frappe.db.delete("__Auth", {"doctype": SETTINGS})
	frappe.delete_doc("DocType", SETTINGS, ignore_missing=True, force=True, ignore_permissions=True)  # authz-ok: tier-a — migration, runs as Administrator at migrate
	frappe.clear_cache()
