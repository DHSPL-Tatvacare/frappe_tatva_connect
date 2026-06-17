"""Register the Push Notifications Module Def on an EXISTING site.

`register_modules` (pre_model_sync) already ran on prior migrates, so a module added
to modules.txt LATER never gets its Module Def — and its doctypes (CRM Push Settings /
CRM Push Subscription) then fail to sync (their required `module` Link is unresolvable).

Re-run add_module_defs here, FIRST in [pre_model_sync], BEFORE model sync, so the new
module exists when its doctypes import in the SAME migrate. Idempotent (ignore_if_duplicate)
and a no-op on a fresh install (install-app already created the defs).
"""
import frappe
from frappe.installer import add_module_defs


def execute():
	add_module_defs("tatva_connect", ignore_if_duplicate=True)
