"""Phase 9: DROP the retired CRM Lead API Field grain columns (grain_vertical/group/program). The per-grain
internal contracts are now the primary seed (access/internal_contract_seed.GRAIN_FIELDS); no code reads these
columns. Frappe never auto-drops a removed field's column — declare the end state, guard by has_column, idempotent."""
import frappe

from tatva_connect.patches import _schema

CATALOG = "CRM Lead API Field"
_COLUMNS = ("grain_vertical", "grain_group", "grain_program")


def execute():
	for column in _COLUMNS:
		if frappe.db.has_column(CATALOG, column):
			# authz-ok: tier-a — migration, runs as Administrator at migrate
			_schema.ddl(f"ALTER TABLE `tabCRM Lead API Field` DROP COLUMN `{column}`", "tabCRM Lead API Field")
