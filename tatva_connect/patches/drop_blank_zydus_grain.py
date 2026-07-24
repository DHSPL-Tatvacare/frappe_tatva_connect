# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Drop the undeclared blank-program Zydus grain.

correct_zydus_grain_vertical removed the two wrong-vertical names ('Goodflip::Zydus::*'), but a
('Goodflip-Care','Zydus','') row survives on a site whose chain landed the blank tuple before it was trimmed
from GRAINS. That composite name is declared nowhere — not in backfill_crm_grain.GRAINS, not in the grain
seed — and correct_zydus_grain_vertical is already applied, so its _STALE list can never reach it. Hence
this new line.

The registry is read as an allow-list (lead/filters.py for the allowed filter set, lead/leads.py for grain
validation), so an undeclared row lets a lead be filed under a slice the config never declared. No lead,
contract or mapping references it, so the drop strands nothing.

Idempotent, and a no-op on a fresh site where the name was never seeded. Assumes nothing about what ran
before.
"""
import frappe

from tatva_connect.patches import backfill_crm_grain

# Declared by neither backfill_crm_grain.GRAINS nor the grain seed — drift left by the trimmed blank tuple.
_ORPHAN = "Goodflip-Care::Zydus::"


def execute():
	if frappe.db.exists("CRM Grain", _ORPHAN):
		frappe.delete_doc("CRM Grain", _ORPHAN, ignore_permissions=True, force=True)  # authz-ok: tier-c — migrate/patch, no session user
		frappe.db.commit()
	backfill_crm_grain.ensure_grains()  # re-assert the declared set after the drop
