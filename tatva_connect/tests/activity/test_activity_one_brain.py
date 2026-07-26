# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Activities has ONE brain, and every consumer reads it.

`CRM Task Type` is the contract — the grain IS its key — and `CRM Task Type Field` is its schema. The
partner API asks it (`api/partner_activity.py`). Smart Views did not: it read 397 rows in the LEAD
catalog, a frozen copy of that schema generated at seed time by a pure function of it, and addressed
by `applies_to` — a Data field holding a foreign key as TEXT.

Then `rekey_task_types_composite` renamed every type. Every real Link cascaded. The text did not.
Measured on this bench before the change: 45 scopes in the copy, 59 buildable from the brain,
INTERSECTION 0 — every Activity Smart View resolved ZERO fields. It was invisible only because no
Activity Smart View had been authored yet.

The copy is deleted and `applies_to` with it. What makes the bug unable to recur is not the delete: it
is that an activity field is now reached ONLY through `CRM Task Type Field`'s real parent FK, and a
rename carries a parent FK. That is what `test_renaming_a_task_type_does_not_orphan_its_fields` pins.
"""
import ast
import os

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.activity import api as activity_brain
from tatva_connect.smartview import api
from tatva_connect.tests.activity import task_type_fixture

# The AST walkers are the sibling module's, imported rather than restated: one scanner, one meta-test
# proving it sees, and a rename there breaks the import here loudly instead of silently scanning nothing.
from tatva_connect.tests.activity.test_activity_brain_integrity import _dotted, _source_files

CATALOG = "CRM Lead API Field"
SECTION = "CRM Lead Section"

# The copy's two mechanisms: `applies_to` addressed it, `sql_source` routed it. Both die with it, so a
# source naming either against this table is rebuilding the copy.
_DEAD_KEYS = ("applies_to", "sql_source")
_ACTIVITY_DOCTYPE = "CRM Task"

TYPE_NAME = "ZZ One Brain Probe"
RENAME_FROM, RENAME_TO = "ZZ Rename Probe", "ZZ Rename Probe Renamed"

# Two fields that name a promoted CRM Task column and two that name none — the ONE routing rule the
# brain writes by and the composer must read by. A fixture carrying only payload fields could not tell
# a correct split from a broken one.
SCHEMA = (
	{"label": "ZZ Outcome", "fieldname": "zz_outcome", "fieldtype": "Data", "target": "custom_outcome"},
	{"label": "ZZ Followup", "fieldname": "zz_followup", "fieldtype": "Datetime", "target": "custom_followup_at"},
	{"label": "ZZ Note", "fieldname": "zz_note", "fieldtype": "Small Text"},
	{"label": "ZZ Cycle", "fieldname": "zz_cycle", "fieldtype": "Data"},
)


def _keys(rows):
	return {r["field_key"] for r in rows}


def _activity_catalog(task_type):
	return api.field_catalog(base_object="Activity", activity_type=task_type)


# ---------------------------------------------------------------------------
# The AST lock — G4: no consumer materialises a copy of another resource's brain.
# ---------------------------------------------------------------------------

def _literal_dict_value(node, key):
	"""The constant value of `key` in a `{...}` display or a `dict(...)` call, else None."""
	if isinstance(node, ast.Dict):
		for k, v in zip(node.keys, node.values, strict=False):
			if isinstance(k, ast.Constant) and k.value == key and isinstance(v, ast.Constant):
				return v.value
	if isinstance(node, ast.Call) and _dotted(node.func) == "dict":
		for kw in node.keywords:
			if kw.arg == key and isinstance(kw.value, ast.Constant):
				return kw.value.value
	return None


def _literal_dict_keys(node):
	"""The literal keys of a `{...}` display or a `dict(...)` call. Honest limit: a key assembled at
	runtime is invisible here — this catches a DECLARATION, which is the only way the copy was built."""
	if isinstance(node, ast.Dict):
		return {k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}
	if isinstance(node, ast.Call) and _dotted(node.func) == "dict":
		return {kw.arg for kw in node.keywords if kw.arg}
	return set()


def _catalog_dicts(call):
	"""Every dict literal this Call hands to a CRM Lead API Field write.

	AST, never grep: the doctype name appears in prose, in filters and in read paths far more often
	than in a write, and only a write can plant a row."""
	if not _dotted(call.func).endswith(("get_doc", "new_doc", "set_value")):
		return []
	# get_doc({"doctype": CATALOG, ...}) / get_doc(dict(doctype=CATALOG, ...))
	out = [a for a in call.args if _literal_dict_value(a, "doctype") == CATALOG]
	# set_value(CATALOG, name, {...})
	if call.args and isinstance(call.args[0], ast.Constant) and call.args[0].value == CATALOG:
		out += [a for a in call.args[1:] if isinstance(a, ast.Dict)]
	return out


def _activity_rows_in(rel, tree):
	"""`file:line — why` for every catalog write in this tree that declares a row which is not a lead
	field: one targeting CRM Task, or one naming a key the copy was addressed or routed by."""
	out = []
	for node in ast.walk(tree):
		if not isinstance(node, ast.Call):
			continue
		for d in _catalog_dicts(node):
			dead = sorted(_literal_dict_keys(d) & set(_DEAD_KEYS))
			if dead:
				out.append(f"{rel}:{node.lineno} names {', '.join(dead)}")
			if _literal_dict_value(d, "target_doctype") == _ACTIVITY_DOCTYPE:
				out.append(f"{rel}:{node.lineno} targets {_ACTIVITY_DOCTYPE}")
	return sorted(out)


def _catalog_write_sites():
	"""`module:function` for every non-test function that writes a CRM Lead API Field row."""
	out = set()
	for rel, tree in _source_files():
		module = rel[: -len(".py")].replace(os.sep, ".")
		for node in ast.walk(tree):
			if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
				continue
			if any(_catalog_dicts(c) for c in ast.walk(node) if isinstance(c, ast.Call)):
				out.add(f"{module}:{node.name}")
	return out


def _activity_row_writers():
	return sorted(w for rel, tree in _source_files() for w in _activity_rows_in(rel, tree))


class TestNothingRebuildsTheCopy(FrappeTestCase):
	"""G4 — nothing writes activity rows into the lead catalog."""

	def test_the_write_scanner_sees_a_real_catalog_writer(self):
		"""The premise. `catalog_seed.ensure_rows` is the one non-test writer of this table: if the
		scanner cannot see the writer we KNOW exists, every assertion below passes on an empty set."""
		self.assertIn("lead_sync.catalog_seed:ensure_rows", _catalog_write_sites(),
					  "the CRM Lead API Field write scanner is blind — it missed catalog_seed.ensure_rows")

	def test_the_activity_row_detector_catches_a_planted_copy(self):
		"""The second premise. A detector that flags nothing keeps this lock green while the copy is
		rebuilt beside it — so it is shown a row of exactly the shape the 397 had."""
		planted = ast.parse(
			'frappe.get_doc({"doctype": "CRM Lead API Field", "field_key": "a208:outcome",\n'
			'                "target_doctype": "CRM Task", "sql_source": "task",\n'
			'                "applies_to": "activity:Welcome Call"}).insert()'
		)
		self.assertTrue(_activity_rows_in("planted.py", planted),
						"the activity-row detector is blind — it passed a verbatim copy row")

	def test_the_activity_row_detector_passes_a_lead_row(self):
		"""And it must not simply flag everything, or the lock says nothing about what it caught."""
		ok = ast.parse(
			'frappe.get_doc({"doctype": "CRM Lead API Field", "field_key": "lead:first_name",\n'
			'                "section": "lead", "target_doctype": "CRM Lead",\n'
			'                "fieldname": "first_name"}).insert()'
		)
		self.assertEqual(_activity_rows_in("ok.py", ok), [],
						 "the detector flagged an ordinary lead catalog row")

	def test_no_source_writes_an_activity_row_into_the_lead_catalog(self):
		writers = _activity_row_writers()
		self.assertEqual(
			writers, [],
			"a catalog row that is not a lead field is being declared. An activity's fields live on "
			"CRM Task Type Field, behind a real parent FK — the lead catalog holds lead fields only. "
			"Ask the activity brain instead:\n  " + "\n  ".join(writers),
		)


class TestTheCopyIsGone(FrappeTestCase):
	"""The 397 rows, and the two columns that addressed and routed them."""

	def test_no_lead_section_targets_crm_task(self):
		"""`target_doctype` now lives on the section, not the catalog row. A CRM Task row would need a
		section that targets CRM Task; no lead section does — so the 397 rows can have no home. Together
		with test_every_catalog_row_is_a_lead_field (every row resolves to a section) that is airtight."""
		self.assertEqual(
			frappe.get_all(SECTION, filters={"target_doctype": _ACTIVITY_DOCTYPE}, pluck="name"), [])

	def test_every_catalog_row_is_a_lead_field(self):
		"""The stronger form: not "no CRM Task rows" but "every row IS a lead field". A row whose
		section does not resolve is a row with no table, no target and no row key — the 397's shape."""
		sections = set(frappe.get_all(SECTION, pluck="name"))
		orphans = [r.name for r in frappe.get_all(CATALOG, fields=["name", "section"])
				   if r.section not in sections]
		self.assertEqual(orphans[:5], [], f"{len(orphans)} catalog rows resolve to no lead section")

	def test_the_columns_the_copy_was_addressed_and_routed_by_are_gone(self):
		"""`applies_to` is the defect itself — a foreign key as text, which is why a rename orphaned
		397 rows. `sql_source` is pure derivation off the section once every row is a lead field."""
		for column in _DEAD_KEYS:
			self.assertFalse(frappe.db.has_column(CATALOG, column),
							 f"`{column}` still exists on tab{CATALOG}")
			self.assertIsNone(frappe.get_meta(CATALOG).get_field(column),
							  f"`{column}` is still a docfield of {CATALOG}")


class TestSmartViewsReadsTheBrain(FrappeTestCase):
	"""B1 — and the identity of source that closes it."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.task_type = task_type_fixture.mint_type(TYPE_NAME, SCHEMA)

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		task_type_fixture.teardown()
		super().tearDownClass()

	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		frappe.set_user("Administrator")

	def test_an_activity_smart_view_resolves_a_non_zero_field_set(self):
		"""B1. Zero today, on every activity type that exists."""
		self.assertTrue(_activity_catalog(self.task_type),
						"an Activity Smart View resolves ZERO fields")

	def test_the_activity_field_set_is_the_brains_schema(self):
		"""Identity of SOURCE, not a list that agrees today. What the composer offers IS get_schema()."""
		schema = activity_brain.get_schema(self.task_type)
		self.assertEqual(
			_keys(_activity_catalog(self.task_type)),
			{f"activity:{f['fieldname']}" for f in schema},
			"the composer's activity fields are not the brain's schema",
		)

	def test_a_field_added_to_the_type_appears_with_no_seed_run(self):
		"""The whole difference between a brain and a copy: the copy needed a generator re-run."""
		before = _keys(_activity_catalog(self.task_type))
		doc = frappe.get_doc("CRM Task Type", self.task_type)
		doc.append("schema", {"label": "ZZ Late", "fieldname": "zz_late", "fieldtype": "Data"})
		doc.save(ignore_permissions=True)
		self.assertEqual(_keys(_activity_catalog(self.task_type)) - before, {"activity:zz_late"})

	def test_every_field_is_addressed_where_field_target_says_and_all_of_them_are_queryable(self):
		"""The ONE routing rule, now `field_target`: a retained common CRM Task column stays the task row,
		everything else is the section row that addresses it. Phase 4 of the task-sections plan — the
		composer must read by that seam or project a column that never fills. There is no display-only
		side left: every declared field is a real column somewhere, so every one of them is queryable."""
		by_key = {r["field_key"]: r for r in _activity_catalog(self.task_type)}
		self.assertEqual(by_key["activity:zz_outcome"]["fieldname"], "custom_outcome")
		self.assertEqual(by_key["activity:zz_outcome"]["sql_source"], "task")
		self.assertEqual(by_key["activity:zz_note"]["fieldname"], "zz_note",
						 "a field with no shape of its own is addressed by its own fieldname")
		for key, row in by_key.items():
			self.assertTrue(row["filterable"], f"{key} still cannot be filtered")
			self.assertTrue(row["sortable"], f"{key} still cannot be sorted")

	def test_the_fieldtype_is_the_schemas_own(self):
		"""The picker's operator menu and value widget read it; the copy could only ever guess 'Data'."""
		by_key = {r["field_key"]: r for r in _activity_catalog(self.task_type)}
		self.assertEqual(by_key["activity:zz_note"]["fieldtype"], "Small Text")


class TestARenameCannotOrphanAnActivityField(FrappeTestCase):
	"""B1's ROOT CAUSE — pinned so it cannot recur. This is the whole point of the change."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.task_type = task_type_fixture.mint_type(RENAME_FROM, SCHEMA)

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		task_type_fixture.teardown()
		super().tearDownClass()

	def setUp(self):
		frappe.set_user("Administrator")

	def test_renaming_a_task_type_does_not_orphan_its_fields(self):
		"""`rekey_task_types_composite` did exactly this to all 59 types and left the copy behind. The
		fields hang off the type's real parent FK now, and a rename carries a parent FK."""
		before = _keys(_activity_catalog(self.task_type))
		self.assertTrue(before, "nothing to orphan — the fixture resolved no fields before the rename")

		renamed = task_type_fixture.key_for(RENAME_TO)
		frappe.rename_doc("CRM Task Type", self.task_type, renamed)
		task_type_fixture.track("CRM Task Type", renamed)
		frappe.db.commit()

		after = _keys(_activity_catalog(renamed))
		self.assertEqual(after, before, "a rename orphaned the type's activity fields — B1, again")
