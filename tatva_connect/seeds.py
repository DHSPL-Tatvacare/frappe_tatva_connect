"""Idempotent master-data seeding, run on after_migrate.

`bench install-app` BASELINES patches.txt (marks them applied) WITHOUT running it,
so master data seeded via patches never lands on a brand-new DB — a fresh-VM /
disaster-recovery rebuild comes up with no India cities, no lead stages, an empty
partner-API catalog, no intake config, and a checklist layout without the checklist.

Run the seeds on after_migrate instead. after_migrate fires AFTER fixtures (so the
masters these seeds Link to — Vertical/Group/Program/Lead Source — already exist) and
AFTER patches, on BOTH a fresh install (install-app then migrate) and every existing-DB
redeploy. All seeds are idempotent, so re-running each migrate is a no-op.

Each seed is isolated: a failure is rolled back, logged, and skipped — a single seed
gap never aborts the migrate/deploy (the gap shows up in Error Log instead).
"""
import frappe

from tatva_connect.patches import seed_india_cities

# Pure config-implementation mode: business/master DATA is NOT auto-seeded. It ships as
# manual SQL in db-seeds/ (operator runs post-migrate, then activates via a Settings form).
# Only INTRINSIC reference data that is identical in every deployment stays here: the India
# city+state list (ODbL) backing the State->City cascade. Everything else (taxonomy, partner-
# API catalog, lead stages, task types, UI layouts) was moved to db-seeds/ on 2026-06-11.
_SEEDS = (seed_india_cities,)


def seed_master_data():
	for mod in _SEEDS:
		try:
			mod.execute()
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(frappe.get_traceback(), f"seed_master_data: {mod.__name__}")
