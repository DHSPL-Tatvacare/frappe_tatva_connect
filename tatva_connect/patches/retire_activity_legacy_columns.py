"""Collapse the activity engine onto 9 promoted CRM Task columns: merge/rename legacy columns into the new fixture columns and retire the dead archetype child tables; order-safe (skips merges before the target syncs), idempotent, forward-only."""
import frappe

from tatva_connect.patches import _schema

TASK = "CRM Task"

# old column -> merged target; merge runs only when BOTH columns exist, copying only where the target is empty (type-safe via CAST+NULLIF).
_MERGES = (
	("custom_visit_status", "custom_outcome"),
	("custom_next_visit_at", "custom_followup_at"),
	("custom_demo_scheduled_at", "custom_scheduled_at"),
)

# Stale fields with no merge, always safe to retire (custom_key_date column + the 4 dead Table links — only their Custom Field docs are removed).
_STALE_FIELDS = (
	"custom_key_date",
	"custom_order_detail",
	"custom_clinical_detail",
	"custom_call_detail",
	"custom_doc_detail",
)

_DEAD_CHILD_DOCTYPES = (
	"CRM Task Order Detail",
	"CRM Task Clinical Detail",
	"CRM Task Call Detail",
	"CRM Task Doc Detail",
)


def _fits(old, new):
	"""The source expression, truncated to the target's width when the target is a character column.

	`custom_visit_status` was a free Data column and `custom_outcome` is a fixture-declared one; a single
	legacy value longer than the target is a 1406 that aborts the whole migrate. Truncating keeps the
	value (visibly clipped) instead of losing the migrate. A Datetime target has no width and is copied
	whole — LEFT() on one would corrupt every row.
	"""
	width = frappe.db.sql(
		"""SELECT CHARACTER_MAXIMUM_LENGTH FROM information_schema.COLUMNS
		   WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'tabCRM Task' AND COLUMN_NAME = %s""",
		(new,),
	)
	limit = width[0][0] if width else None
	return f"LEFT(`{old}`, {int(limit)})" if limit else f"`{old}`"


def _retire_column(fieldname):
	"""Drop a retired CRM Task field end to end: delete the Custom Field doc AND the physical column (deletion alone leaves the column); a Table field has no parent column, so only its doc is removed."""
	cf = "CRM Task-" + fieldname
	if frappe.db.exists("Custom Field", cf):
		frappe.delete_doc("Custom Field", cf, ignore_permissions=True, force=True)  # authz-ok: tier-a — migration, runs as Administrator at migrate
	if frappe.db.has_column(TASK, fieldname):
		# sql_ddl, not sql: a bare ALTER trips frappe's implicit-commit guard on the after_migrate path.
		_schema.ddl(f"ALTER TABLE `tabCRM Task` DROP COLUMN `{fieldname}`", "tabCRM Task")


def execute():
	# 1) Carry values into the merged/renamed columns then retire the old, but ONLY once the target column exists (else leave it for the after_migrate pass); NULLIF(CAST(...)) treats NULL and '' as empty for Data + Datetime.
	for old, new in _MERGES:
		if not (frappe.db.has_column(TASK, old) and frappe.db.has_column(TASK, new)):
			continue
		frappe.db.sql(
			f"""UPDATE `tabCRM Task`
			   SET `{new}` = {_fits(old, new)}
			   WHERE NULLIF(CAST(`{old}` AS CHAR), '') IS NOT NULL
			     AND NULLIF(CAST(`{new}` AS CHAR), '') IS NULL"""
		)
		_retire_column(old)

	# 2) Retire the stale fields with no merge (custom_key_date + the 4 dead Table links).
	for fieldname in _STALE_FIELDS:
		_retire_column(fieldname)

	# 3) Drop the 4 dead archetype child doctypes + their tables (folders archived to archive/doctype/).
	for dt in _DEAD_CHILD_DOCTYPES:
		if frappe.db.exists("DocType", dt):
			frappe.delete_doc("DocType", dt, ignore_permissions=True, force=True)  # authz-ok: tier-a — migration, runs as Administrator at migrate
