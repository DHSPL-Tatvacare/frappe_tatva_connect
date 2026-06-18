# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""Retire the old catch-all "Tatva Connect" desk space.

It was split into six focused spaces (Communications, Automations, Partner API,
Infrastructure, Field Operations, Observability), each shipped as a Workspace +
Workspace Sidebar fixture. The obsolete single records are removed here so they
don't linger in the app switcher. Idempotent; no-op on a clean install.
"""

import frappe


def execute():
	for dt in ("Workspace", "Workspace Sidebar"):
		if frappe.db.exists(dt, "Tatva Connect"):
			frappe.delete_doc(dt, "Tatva Connect", force=True, ignore_permissions=True)
