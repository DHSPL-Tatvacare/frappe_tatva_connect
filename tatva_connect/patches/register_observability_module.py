"""Re-register Module Defs (pre-model-sync) so the Observability module added to modules.txt later exists before its doctypes sync; idempotent."""
from frappe.installer import add_module_defs


def execute():
	add_module_defs("tatva_connect", ignore_if_duplicate=True)
