"""Fresh-install seeding.

`bench install-app` BASELINES everything in patches.txt (marks them applied)
WITHOUT running them, so master data that is seeded via patches never lands on a
brand-new database. That breaks a fresh-VM / disaster-recovery rebuild: the app
comes up with no India cities, no lead stages, an empty partner-API catalog, no
intake config, and a checklist layout without the checklist.

Fix: run the (idempotent) seeds here, on install. This hook runs ONLY on a fresh
install — an existing prod redeploy runs `bench migrate` (not install), so these
do NOT re-run there and there is zero duplication risk; that data already exists
from when the patches first ran.
"""
import frappe

from tatva_connect.patches import (
	seed_automation_task_types,
	seed_india_cities,
	seed_lead_api_fields,
	seed_lead_stages,
	seed_nivolumab_intake,
	set_task_quick_entry_layout,
)


def after_install():
	for mod in (
		seed_automation_task_types,  # CRM Task Type rows the automations reference
		seed_lead_stages,            # per-program CRM Lead Stage lifecycles
		seed_india_cities,           # CRM City master
		seed_lead_api_fields,        # partner-API field catalog (Lead API Field)
		seed_nivolumab_intake,       # intake form + sample masters
		set_task_quick_entry_layout, # checklist + task type in the CRM Task modal
	):
		mod.execute()
	frappe.db.commit()
