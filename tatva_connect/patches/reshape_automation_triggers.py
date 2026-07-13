"""Automation engine v2 (Task 1, big-bang reshape - invariant A.8, no parallel trigger model):
migrate CRM Automation Rule off the flat trigger_type/task_type/watch_doctype/watch_field vocabulary
onto the two-axis trigger (on_doctype, event) BEFORE the new JSON drops the old columns (pre-model-
sync — same idiom as patches.migrate_intake_field_map_target: raw ADD COLUMN for the not-yet-synced
destination columns, then migrate values via frappe.db.set_value, never through Document.save() so a
still-old-symbol sibling criterion can't trip the (already-reshaped) validate() mid-migration).

Mapping (plan Part C / Task 1):
  trigger_type=="Task Completed" -> on_doctype="CRM Task", event="Updated", PREPEND a criterion
    `status changed to Done` (idx 0) - this is the exact "task completed" grammar the plan's no-
    sugar-labels rule spells out (Part A/Global Constraints). If the old row also carried a
    non-empty `task_type` (the OLD engine only fired a Task-Completed rule when the completed
    task's type matched it - dispatcher matched `doc.custom_task_type == rule.task_type`), ALSO
    prepend `custom_task_type is <task_type>` so the v2 rule keeps that scoping (invariant A.7 -
    the composite `::` value is carried over verbatim, never stripped). Without this a migrated
    Task-Completed rule would fire on ANY CRM Task completing, not just its original task type.
  trigger_type=="Field Changed" -> on_doctype=<old watch_doctype>, event="Updated".

Also remaps CRM Automation Criterion's old operator SYMBOLS (=, !=, <, >, <=, >=, like, not like, in,
not in, is set, is unset, between, changed_from_to) to the frozen v2 WORD operators (Part A) -
independent of the rule-shape migration above (same column, values only), so it runs unconditionally
for every existing criterion row, not just ones on a migrated rule.

Idempotent: guarded by has_column(trigger_type) / has_column(operator-is-old-symbol) so a re-run
after the JSON has synced (old columns gone, values already words) is a clean no-op. Reconciles the
CRM Automation Rule row count before/after - frappe.throw on any mismatch (never silently drop a row).

Column retirement (A.14): `bench migrate` never drops a column removed from a doctype JSON, so
trigger_type/task_type/watch_doctype/watch_field would otherwise linger as dead schema forever.
Once the reconcile guard above confirms every value is safely on the v2 shape, `_migrate_rules`
calls `_drop_legacy_columns()` to DROP each of the four old-vocab columns that's still physically
present (one small helper, A.8 - no per-column duplicated DDL).
"""
import frappe

from tatva_connect.patches import _schema

_RULE = "CRM Automation Rule"
_RULE_TABLE = "tab" + _RULE
_CRITERION = "CRM Automation Criterion"
_CRITERION_TABLE = "tab" + _CRITERION

# The four old-vocab columns retired by this reshape (docstring "Column retirement").
_LEGACY_COLUMNS = ("trigger_type", "task_type", "watch_doctype", "watch_field")

# Old operator SYMBOL -> new v2 WORD operator (Part A). changed_from_to (the old single Field-Changed
# transition operator) maps onto the new paired "changed from…to" - the closest word-operator analogue.
_OPERATOR_MAP = {
	"=": "is", "!=": "is not", "<": "less than", ">": "greater than",
	"<=": "at most", ">=": "at least", "like": "contains", "not like": "does not contain",
	"in": "is one of", "not in": "is not one of",
	"is set": "is set", "is unset": "is not set", "between": "is between",
	"changed_from_to": "changed from…to",
}


def _column_exists(table, column):
	"""True if the column physically exists; read from information_schema (has_column can report
	stale after a raw DDL change earlier in this same migrate)."""
	return bool(
		frappe.db.sql(
			"""SELECT 1 FROM information_schema.COLUMNS
			   WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s""",
			(table, column),
		)
	)


def execute():
	if not frappe.db.table_exists(_RULE):
		return  # fresh install — the v2 schema is the only one; nothing to carry.
	# ALLOWLIST: _column_exists (information_schema), not frappe.db.has_column — has_column caches
	# per-table column lists that go stale after a raw DDL change (ours, or an earlier patch's) in
	# the same process/migrate.
	if _column_exists(_RULE_TABLE, "trigger_type"):
		_migrate_rules()
	if frappe.db.table_exists(_CRITERION):
		_migrate_criteria_operators()
	frappe.db.commit()


def _migrate_rules():
	before = frappe.db.count(_RULE)

	# ALLOWLIST: raw ADD COLUMN DDL pre-model-sync — no Frappe helper (the JSON hasn't synced yet).
	if not _column_exists(_RULE_TABLE, "on_doctype"):
		_schema.ddl(f"ALTER TABLE `{_RULE_TABLE}` ADD COLUMN `on_doctype` varchar(140)", f"{_RULE_TABLE}")
	if not _column_exists(_RULE_TABLE, "event"):
		_schema.ddl(f"ALTER TABLE `{_RULE_TABLE}` ADD COLUMN `event` varchar(140)", f"{_RULE_TABLE}")

	# task_type/watch_doctype may already be individually absent on a site whose columns drifted
	# ahead of trigger_type (e.g. a partially-cleaned dev DB) — select NULL in their place rather
	# than a static column list that would fail to parse against the real table (same drift
	# tolerance _column_exists exists for elsewhere in this file).
	task_type_expr = "task_type" if _column_exists(_RULE_TABLE, "task_type") else "NULL AS task_type"
	watch_doctype_expr = "watch_doctype" if _column_exists(_RULE_TABLE, "watch_doctype") else "NULL AS watch_doctype"
	rows = frappe.db.sql(
		f"""SELECT name, trigger_type, {task_type_expr}, {watch_doctype_expr} FROM `{_RULE_TABLE}`
		    WHERE COALESCE(on_doctype, '') = ''""",
		as_dict=True,
	)
	for r in rows:
		if r.trigger_type == "Task Completed":
			frappe.db.set_value(_RULE, r.name, {"on_doctype": "CRM Task", "event": "Updated"}, update_modified=False)
			if r.task_type:
				# A.7: store the composite `::` task-type value verbatim - never stripped. Prepended
				# first so it lands under `status` (inserted second, below) once both are at idx 0/1.
				_prepend_criterion(r.name, "custom_task_type", "is", r.task_type)
			_prepend_criterion(r.name, "status", "changed to", "Done")
		elif r.trigger_type == "Field Changed":
			frappe.db.set_value(
				_RULE, r.name, {"on_doctype": r.watch_doctype, "event": "Updated"}, update_modified=False
			)
		# else: trigger_type blank/unrecognised (shouldn't happen — it was `reqd`) — leave
		# on_doctype/event blank; the reshaped validate() rejects it as an incomplete trigger the
		# next time the row is opened, fail-loud rather than a silent guess.

	after = frappe.db.count(_RULE)
	if after != before:
		frappe.throw(
			f"reshape_automation_triggers: CRM Automation Rule row count changed {before} -> {after} "
			"during migration — refusing to continue."
		)

	# Only drop once the reconcile guard above has confirmed no row was silently lost.
	_drop_legacy_columns()


def _drop_legacy_columns():
	"""One drop helper (A.8) for the four retired old-vocab columns - iterates _LEGACY_COLUMNS
	instead of four copy-pasted DROP COLUMN blocks. Idempotent: _column_exists skips a column
	already dropped by an earlier run."""
	to_drop = [col for col in _LEGACY_COLUMNS if _column_exists(_RULE_TABLE, col)]
	if not to_drop:
		return
	# ALLOWLIST: innodb_strict_mode OFF, scoped to this DROP only - the table's off-page TEXT
	# columns (description/_comments/_assign/...) get miscounted as inline by InnoDB's row-size
	# validator on this DROP COLUMN algorithm, tripping the 8126-byte ceiling even though the
	# post-drop row is strictly smaller. Session-only toggle, restored immediately after.
	frappe.db.sql("SET SESSION innodb_strict_mode = OFF")
	try:
		for col in to_drop:
			# ALLOWLIST: raw DROP COLUMN DDL, same idiom as the ADD COLUMN above - schema-only,
			# no value interpolation (S.2). Runs AFTER the row-count reconcile guard, i.e. only
			# once every value has been confirmed migrated onto the v2 columns.
			_schema.ddl(f"ALTER TABLE `{_RULE_TABLE}` DROP COLUMN `{col}`", f"{_RULE_TABLE}")
	finally:
		frappe.db.sql("SET SESSION innodb_strict_mode = ON")


def _prepend_criterion(rule_name, field, operator, value):
	"""One insert brain (invariant A.8) for every criterion this patch prepends onto a migrated
	rule — e.g. the no-sugar-labels grammar for the old "Task Completed" trigger (plan Global
	Constraints): `On CRM Task Updated · If status changed to Done`, and (also Task Completed, when
	the old row carried a task_type) `If custom_task_type is <task_type>` to preserve the OLD
	engine's per-task-type scoping. Raw SQL, not Document.save() — a sibling criterion still holding
	an old operator SYMBOL (not yet remapped by _migrate_criteria_operators) would trip the already-
	reshaped CRMAutomationRule.validate() mid-migration. Inserts at idx 0, shifting the rest."""
	if frappe.db.exists(_CRITERION, {"parent": rule_name, "parenttype": _RULE, "field": field, "operator": operator, "value": value}):
		return  # idempotent — already prepended by an earlier run of this patch
	frappe.db.sql(
		f"UPDATE `{_CRITERION_TABLE}` SET idx = idx + 1 WHERE parent = %(parent)s AND parenttype = %(parenttype)s",
		{"parent": rule_name, "parenttype": _RULE},
	)
	now = frappe.utils.now_datetime()
	frappe.db.sql(
		f"""INSERT INTO `{_CRITERION_TABLE}`
		    (name, parent, parenttype, parentfield, idx, field, operator, `value`, from_value,
		     creation, modified, modified_by, owner, docstatus)
		    VALUES (%(name)s, %(parent)s, %(parenttype)s, 'criteria', 0, %(field)s, %(operator)s, %(value)s, '',
		            %(now)s, %(now)s, 'Administrator', 'Administrator', 0)""",
		{
			"name": frappe.generate_hash(length=10), "parent": rule_name, "parenttype": _RULE,
			"field": field, "operator": operator, "value": value, "now": now,
		},
	)


def _migrate_criteria_operators():
	"""Remap every existing criterion's old operator SYMBOL to its v2 WORD equivalent. Column-value-
	only (no new column) — runs independently of the rule-shape migration and unconditionally over
	every criterion row (not just ones on a migrated rule), so it's safe to re-run after a partial
	failure and idempotent once every row already holds a word operator."""
	rows = frappe.db.sql(
		f"SELECT name, operator FROM `{_CRITERION_TABLE}` WHERE operator IS NOT NULL AND operator != ''",
		as_dict=True,
	)
	for r in rows:
		new_op = _OPERATOR_MAP.get(r.operator)
		if new_op and new_op != r.operator:
			frappe.db.set_value(_CRITERION, r.name, "operator", new_op, update_modified=False)
