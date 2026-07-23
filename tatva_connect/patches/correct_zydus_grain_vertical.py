# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Correct the Zydus grains' vertical from Goodflip to Goodflip-Care.

add_zydus_liver_forever_grain seeded ('Goodflip','Zydus','') and ('Goodflip','Zydus','Liver-Forever'); the
curated grain is (Goodflip-Care, Zydus, Liver-Forever). That patch already ran on migrated sites and an
applied patch is dead, so its corrected GRAINS list never re-lands there — hence this new line. It drops
the two stale CRM Grain rows and lets ensure_grains re-create them from the corrected list.

Idempotent, and a no-op on a fresh site: the stale composite names never existed there (the seed wrote the
Goodflip-Care rows), so the delete finds nothing and ensure_grains finds the corrected rows already present.
Liver-Forever is greenfield — no lead, contract or mapping references the stale grain — so the drop strands
nothing. Assumes nothing about what ran before.
"""
import frappe

from tatva_connect.patches import backfill_crm_grain

# The wrong-vertical composite names add_zydus_liver_forever_grain left on already-migrated sites.
_STALE = ("Goodflip::Zydus::", "Goodflip::Zydus::Liver-Forever")


def execute():
	for name in _STALE:
		if frappe.db.exists("CRM Grain", name):
			frappe.delete_doc("CRM Grain", name, ignore_permissions=True, force=True)  # authz-ok: tier-c — migrate/patch, no session user
	frappe.db.commit()
	backfill_crm_grain.ensure_grains()  # re-lands ('Goodflip-Care','Zydus',*) from the corrected GRAINS
