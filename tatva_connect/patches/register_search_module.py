"""Re-register Module Defs (pre-model-sync) so the Search module added to modules.txt later exists before CRM Search Alias syncs; idempotent."""
from frappe.installer import add_module_defs


def execute():
	add_module_defs("tatva_connect", ignore_if_duplicate=True)
