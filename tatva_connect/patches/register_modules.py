"""Register modules.txt -> Module Def before model sync (migrate on an existing site won't, so a doctype's required `module` Link would fail); pre-model-sync, idempotent."""
import frappe
from frappe.installer import add_module_defs


def execute():
	add_module_defs("tatva_connect", ignore_if_duplicate=True)
