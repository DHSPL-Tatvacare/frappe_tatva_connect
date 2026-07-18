# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The activity brain is right. These tests pin what got past it.

`CRM Task Type` autonames `format:{vertical}::{group}::{program}::{type_name}`, so the grain IS the
key and the type IS the contract; `CRM Task Type Field` is its schema. Nothing here questions that.
What leaked was enforcement:

  * `CRM Task Type Scope` — a deprecated child table carrying a RIVAL grain, still live-read in three
    places. `_action_create_task` gated the grain check on a Scope row existing, so a type carrying
    only the parent composite grain — that is, every real type — had `scoped` falsy and the check
    NEVER RAN. A rival grain source whose only remaining effect was to switch enforcement off.
  * `create_followup_task` — the one writer that legitimately does not route through
    `compute_activity` (it creates a schema-less shell; there is no submitted form to resolve). That
    does not license it to plant a task outside the lead's grain, and nothing stopped it.
  * `CALL_LEAD_TYPE = "Call Lead"` — a bare string standing where every other type is a composite PK.

The closed state: ONE grain source (the parent composite key), ONE gate (`_scope_applies`, keyed on
the LEAD the task lands on), and every CRM Task writer either routing through `compute_activity` or
named below with its reason.
"""
import ast
import os
import pathlib

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.activity.api import compute_activity
from tatva_connect.automation import actions
from tatva_connect.tasks.tasks import create_followup_task

# The grain is the operator's taxonomy, so this module mints its own rather than naming one it hopes
# the seed carries. Two grains that share NO axis: the gate must refuse across either. Named so no
# operator taxonomy can collide with them, and torn down in full.
VERTICAL, GROUP = "ZZ Activity Line", "ZZ Activity Group"
FOREIGN = ("ZZ Foreign Line", "ZZ Foreign Group", "ZZ Foreign Program")
TYPE_NAME, FOREIGN_TYPE_NAME = "ZZ Fixture Followup", "ZZ Fixture Foreign"
SCHEMA_FIELD = "zz_fixture_note"  # a payload field: it names no promoted column, so 3.6 is deterministic

# .../tatva_connect  (the app package root)
_APP_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_SCOPE_DT = "CRM Task Type Scope"

# Every CRM Task writer that does NOT route through `compute_activity`, and why that is not a bypass.
# A writer absent from here and from the brain fails the build.
_NAMED_EXCEPTIONS = {
	"tasks.tasks:create_followup_task":
		"a schema-less SHELL — an open to-do carries no submitted form, so there is nothing for "
		"compute_activity to resolve; grain-gated by _scope_applies before the insert",
	"api.partner_activity:_backdate":
		"writes `creation` only — a framework column on an already-brained task, never an activity field",
	"automation.actions:_pin_review_file":
		"seeds the review task's `document` payload key from the File that raised it — the value is the "
		"trigger's own file_url, not caller input, and the shell it lands on was already grain-gated",
	"notifications.events:_notify_due":
		"stamps the notified-at marker column so a rep is told once — never an activity field",
}

# A grainless CRM Task Type is dormant by `_grain_matches` and can never be raised, so a bare name is
# a row that lies about being available. None is justified; the mapping exists to force the argument.
_BARE_TYPES_ALLOWED = {}


_MADE = []  # (doctype, name) this module minted, torn down in reverse. Never something it found.


def _make(doctype, name, values):
	if frappe.db.exists(doctype, name):
		return name
	frappe.get_doc({"doctype": doctype, **values}).insert(ignore_permissions=True)
	_MADE.append((doctype, name))
	return name


def _mint_task_type(type_name, vertical, group, program="", schema=()):
	"""A CRM Task Type on a minted grain, with the masters its axes Link to. The key restates the
	doctype's own autoname format (the module docstring names it) so the mint can be idempotent."""
	_make("CRM Vertical", vertical, {"vertical_name": vertical})
	_make("CRM Group", group, {"group_name": group})
	if program:
		_make("CRM Program", program, {"program_name": program})
	return _make("CRM Task Type", f"{vertical}::{group}::{program}::{type_name}", {
		"type_name": type_name, "vertical": vertical, "group": group, "program": program,
		"schema": [{"label": f, "fieldname": f, "fieldtype": "Data"} for f in schema],
	})


def _mint_grain_fixture():
	"""The in-grain type (program-agnostic — a blank axis is a wildcard) and a type sharing no axis."""
	in_grain = _mint_task_type(TYPE_NAME, VERTICAL, GROUP, schema=(SCHEMA_FIELD,))
	foreign = _mint_task_type(FOREIGN_TYPE_NAME, *FOREIGN)
	frappe.db.commit()  # survives the per-test rollback; the gate reads these live
	return in_grain, foreign


def _teardown_grain_fixture():
	for doctype, name in reversed(_MADE):
		if frappe.db.exists(doctype, name):
			frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)
	_MADE.clear()
	frappe.db.commit()


def _dotted(node):
	"""`frappe.db.set_value` from an AST func node, or "" for anything not a plain dotted name."""
	parts = []
	while isinstance(node, ast.Attribute):
		parts.append(node.attr)
		node = node.value
	if not isinstance(node, ast.Name):
		return ""
	parts.append(node.id)
	return ".".join(reversed(parts))


def _docstring_ids(tree):
	"""Node ids of every docstring, so a doctype NAMED in prose is never counted as a reference."""
	out = set()
	for node in ast.walk(tree):
		body = getattr(node, "body", None)
		if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
			continue
		if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
			out.add(id(body[0].value))
	return out


def _source_files(skip_dirs=("tests",)):
	"""Every non-test .py under tatva_connect/, as (module_path, tree)."""
	for path in sorted(pathlib.Path(_APP_DIR).rglob("*.py")):
		rel = os.path.relpath(path, _APP_DIR)
		parts = rel.split(os.sep)[:-1]
		if any(d in parts for d in skip_dirs):
			continue
		yield rel, ast.parse(path.read_text(), filename=str(path))


def _writes_crm_task(call):
	"""True if this Call hands a CRM Task to get_doc / new_doc / db.set_value — i.e. writes one.

	AST, never grep: `notifications/events.py` passes `data={"doctype": "CRM Task", ...}` into a push
	payload. That is the string a grep counts and the doc write it is not."""
	name = _dotted(call.func)
	if not name.endswith(("get_doc", "new_doc", "set_value")) or not call.args:
		return False
	first = call.args[0]
	if isinstance(first, ast.Constant) and first.value == "CRM Task":
		return True
	if not isinstance(first, ast.Dict):
		return False
	return any(
		isinstance(k, ast.Constant) and k.value == "doctype"
		and isinstance(v, ast.Constant) and v.value == "CRM Task"
		for k, v in zip(first.keys, first.values)
	)


def _task_write_sites():
	"""`module:function` for every non-test function that writes a CRM Task, brain-routed or not."""
	out = set()
	for rel, tree in _source_files():
		module = rel[: -len(".py")].replace(os.sep, ".")
		for node in ast.walk(tree):
			if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
				continue
			if any(isinstance(c, ast.Call) and _writes_crm_task(c) for c in ast.walk(node)):
				out.add(f"{module}:{node.name}")
	return out


def _brain_routed():
	"""`module:function` for every function that calls `compute_activity` — the brain's own writers."""
	out = set()
	for rel, tree in _source_files():
		module = rel[: -len(".py")].replace(os.sep, ".")
		for node in ast.walk(tree):
			if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
				continue
			if any(
				isinstance(c, ast.Call) and _dotted(c.func).endswith("compute_activity")
				for c in ast.walk(node)
			):
				out.add(f"{module}:{node.name}")
	return out


def _scope_references():
	"""`file:line` wherever non-test, non-patch source names the Scope doctype — excluding docstrings.

	A patch is exempt by construction: the patch that DELETES the doctype must name it to delete it."""
	out = []
	for rel, tree in _source_files(skip_dirs=("tests", "patches")):
		skip = _docstring_ids(tree)
		for node in ast.walk(tree):
			if isinstance(node, ast.Constant) and node.value == _SCOPE_DT and id(node) not in skip:
				out.append(f"{rel}:{node.lineno}")
	return sorted(out)


class TestTheCRMTaskWriterLock(FrappeTestCase):
	"""3.1 — every CRM Task writer routes through the brain, or is named here with a reason."""

	def test_the_scanner_sees_the_brains_own_writer(self):
		"""The premise. `save_activity` writes CRM Tasks and calls `compute_activity`: if the scanner
		cannot see the one writer we KNOW is both, every assertion below is vacuous."""
		self.assertIn("activity.api:save_activity", _task_write_sites(),
					  "the CRM Task write scanner is blind — it missed save_activity")
		self.assertIn("activity.api:save_activity", _brain_routed(),
					  "the compute_activity scanner is blind — it missed save_activity")

	def test_every_crm_task_writer_is_brained_or_named(self):
		unnamed = sorted(_task_write_sites() - _brain_routed() - set(_NAMED_EXCEPTIONS))
		self.assertEqual(
			unnamed, [],
			"a CRM Task writer neither routes through compute_activity nor is a named exception. "
			"Route it through the brain, or add it to _NAMED_EXCEPTIONS with the reason it is not a "
			"bypass:\n  " + "\n  ".join(unnamed),
		)

	def test_no_named_exception_is_stale(self):
		"""A refactor that removes a bypass must force a re-read of the list that excused it."""
		stale = sorted(set(_NAMED_EXCEPTIONS) - _task_write_sites())
		self.assertEqual(stale, [], f"_NAMED_EXCEPTIONS names a writer that no longer exists: {stale}")


class TestTheScopeRivalIsGone(FrappeTestCase):
	"""3.2 — the rival grain source has zero readers and no table."""

	def test_no_source_reads_the_scope_doctype(self):
		refs = _scope_references()
		self.assertEqual(refs, [], f"{_SCOPE_DT} is a rival grain source and is still read at: {refs}")

	def test_the_scope_table_and_doctype_are_gone(self):
		self.assertFalse(frappe.db.table_exists(_SCOPE_DT), f"tab{_SCOPE_DT} still exists")
		self.assertFalse(frappe.db.exists("DocType", _SCOPE_DT), f"the {_SCOPE_DT} doctype still exists")

	def test_crm_task_type_carries_no_scope_field(self):
		self.assertIsNone(
			frappe.get_meta("CRM Task Type").get_field("scope"),
			"CRM Task Type still carries the deprecated `scope` Table field",
		)


class TestTheGrainGate(FrappeTestCase):
	"""3.3 / 3.4 — the grain gate fires for every writer, on a type with no Scope row (i.e. all of them)."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.in_grain, cls.foreign = _mint_grain_fixture()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		_teardown_grain_fixture()
		super().tearDownClass()

	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		frappe.set_user("Administrator")
		self.lead = frappe.get_doc({
			"doctype": "CRM Lead", "first_name": "Grain Gate Probe",
			"mobile_no": f"+9198126{int(frappe.generate_hash(length=8), 16) % 100000:05d}",
			"custom_vertical": VERTICAL, "custom_group": GROUP,
		}).insert(ignore_permissions=True)

	def _tasks_of(self, task_type):
		return frappe.get_all("CRM Task", filters={
			"reference_doctype": "CRM Lead", "reference_docname": self.lead.name,
			"custom_task_type": task_type,
		}, pluck="name")

	def test_create_followup_task_refuses_an_out_of_grain_type(self):
		"""3.3 — the shell writer skips compute_activity because there is nothing to compute. It does
		not get to skip the grain: a lead may not carry a task type that shares no axis with it."""
		with self.assertRaises(frappe.ValidationError):
			create_followup_task(self.lead.name, self.foreign, throttle=False)
		self.assertEqual(self._tasks_of(self.foreign), [],
						 "an out-of-grain task was planted on the lead despite the refusal")

	def test_create_followup_task_still_raises_an_in_grain_type(self):
		"""The gate must refuse the foreign type because it is foreign — not because it refuses."""
		name = create_followup_task(self.lead.name, self.in_grain, throttle=False)
		self.assertTrue(frappe.db.exists("CRM Task", name))

	def test_the_automation_lane_refuses_an_out_of_grain_type(self):
		"""3.4 — the check was gated on a Scope row EXISTING, and no real type has one: `scoped` was
		falsy, so the guard never ran and a grain-A rule could plant a grain-B type."""
		action = frappe._dict(action_type="Create Task", task_type=self.foreign, due_mode="From Context")
		with self.assertRaises((frappe.ValidationError, PermissionError)):
			actions._action_create_task(action, self.lead.name, {}, (VERTICAL, GROUP, ""), None)
		self.assertEqual(self._tasks_of(self.foreign), [],
						 "the automation lane planted an out-of-grain task")

	def test_the_gate_reads_the_lead_not_the_passed_axes(self):
		"""REGRESSION LOCK — green before and after, and it must stay that way.

		`interpreter._axes` returns (None, None, None) for a Flow whose subject is not a CRM Lead — a
		File-triggered Document Review is exactly that — while still resolving the real parent lead.
		A grain check keyed on that axes tuple therefore refuses EVERY type on the durable path. The
		gate must read the lead the task lands on, which is the one thing always true."""
		action = frappe._dict(action_type="Create Task", task_type=self.in_grain, due_mode="From Context")
		actions._action_create_task(action, self.lead.name, {}, (None, None, None), None)
		self.assertTrue(self._tasks_of(self.in_grain),
						"a File/WhatsApp-subject Flow can no longer raise an in-grain task")


class TestTheBrainOwnsThePayload(FrappeTestCase):
	"""3.5 / 3.6 — compute_activity reads the schema, never the caller's key set. Already true; locked."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.task_type, _foreign = _mint_grain_fixture()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		_teardown_grain_fixture()
		super().tearDownClass()

	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		frappe.set_user("Administrator")
		self.lead = frappe.get_doc({
			"doctype": "CRM Lead", "first_name": "Payload Probe",
			"mobile_no": f"+9198127{int(frappe.generate_hash(length=8), 16) % 100000:05d}",
			"custom_vertical": VERTICAL, "custom_group": GROUP,
		}).insert(ignore_permissions=True)

	def test_a_values_key_absent_from_the_schema_is_dropped(self):
		"""3.5 — the loop iterates tt.schema, so an undeclared key is never read, let alone stored."""
		fields = compute_activity(self.lead.name, self.task_type, {"totally_invented_key": "smuggled"})
		payload = frappe.parse_json(fields["custom_activity_payload"]) or {}
		self.assertNotIn("totally_invented_key", payload)
		self.assertNotIn("smuggled", frappe.as_json(fields))

	def test_a_payload_naming_a_promoted_column_never_reaches_it(self):
		"""3.6 — a caller writing `custom_outcome` directly is naming a column, not a schema field.
		Only a schema field whose `target` IS that column may route there. The minted type declares one
		payload field and targets nothing, so every promoted column below is genuinely undeclared —
		this used to hedge ("pick another") against whatever the seeded type happened to declare."""
		hijack = "HIJACKED-BY-PAYLOAD"
		promoted = ("custom_outcome", "custom_reference", "custom_asm")
		fields = compute_activity(self.lead.name, self.task_type, {c: hijack for c in promoted})
		for column in promoted:
			self.assertNotEqual(fields.get(column), hijack,
								f"a raw payload key wrote straight to the promoted column {column}")


class TestEveryTaskTypeIsKeyedByItsGrain(FrappeTestCase):
	"""3.7 — the composite key IS the contract, so a name that is not one has no grain to be read."""

	def test_every_task_type_name_is_a_composite_pk(self):
		bare = [n for n in frappe.get_all("CRM Task Type", pluck="name") if "::" not in n]
		unjustified = sorted(n for n in bare if n not in _BARE_TYPES_ALLOWED)
		self.assertEqual(
			unjustified, [],
			"a CRM Task Type is named, not keyed by its grain. `_grain_matches` calls an all-blank "
			"grain dormant, so this row can never be raised on any lead — it advertises an activity "
			"that does not exist. Re-key it to its grain, or delete it:\n  " + "\n  ".join(unjustified),
		)
