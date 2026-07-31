# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The operator head: a declaration authored as a ROW is the same citizen a code declaration is.

Four properties, and the first is the one everything else hangs off:

  * ONE REGISTRY, TWO SOURCES. An enabled `CRM Derived Field` row is answered by `for_doctype` / `get` /
    `names` exactly as `fields.py` is, and no consumer can tell which it holds. Asserted by driving the
    lenses and the list, never by reading the registry back.
  * DORMANT. A row with `enabled = 0` changes nothing anywhere — not a menu, not a column, not a version.
    That is this app's standing rule and it is the reason an operator can author safely.
  * CACHED, AND LIVE ON SAVE. `for_doctype` sits on the busiest read path in the app, so the rows are read
    once and cached; `reload()` is what makes a save live, and `declaration_version()` is what makes it
    live in a browser whose lens caches have no expiry.
  * NOTHING THAT SHIPPED MOVES. A code declaration names no `surfaces` and no `default_column`, which read
    as every menu and shown-only-when-added — precisely what those fields did before they existed.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.list_engine.test_derived_registry
"""

import contextlib
import json
import typing

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.api import task_lenses
from tatva_connect.list_engine import derived
from tatva_connect.tests.list_engine.test_list_engine import FIELD, TASK, ListEngineCase, _rows_arg

CLOSED = ["Done", "Canceled"]

BUCKETS = [
	{"value": "Open", "theme": "green", "filters": [["status", "not in", CLOSED]]},
	{"value": "Closed", "theme": "gray", "filters": [["status", "in", CLOSED]]},
]


def _probe(fieldname, **declared):
	"""A sound two-bucket declaration under a probe name, so these tests never disturb the real one."""
	return derived.DerivedField(
		doctype=TASK,
		fieldname=fieldname,
		label="Probe Field",
		buckets=[derived.Bucket(b["value"], b["filters"], b["theme"]) for b in BUCKETS],
		**declared,
	)


@contextlib.contextmanager
def _registered(field):
	"""A code declaration for the duration of one test, removed the way the registry is removed from."""
	derived.register(field)
	try:
		yield field
	finally:
		derived._REGISTRY.get(field.doctype, {}).pop(field.fieldname, None)
		derived._invalidate()


def _authored_row(**overrides):
	"""One `CRM Derived Field` row. Enabled by default here because the dormant case is asserted directly."""
	payload = {
		"doctype": derived.ROW_DOCTYPE,
		"dt": TASK,
		"fieldname": "_probe_authored",
		"label": "Authored Probe",
		"enabled": 1,
		"buckets": json.dumps(BUCKETS),
	}
	payload.update(overrides)
	return frappe.get_doc(payload).insert()


class TestACodeDeclarationDidNotMove(FrappeTestCase):
	"""The no-regression floor for the CODE source, which is still a legitimate one.

	`due_state` is a ROW now, so this registers its own declaration rather than leaning on a citizen that
	moved: a code declaration names no `surfaces`, no `default_column` and no theme, and must therefore
	still describe itself exactly as it did before either field existed."""

	CODE_FIELD = "_code_probe"

	def setUp(self):
		frappe.set_user("Administrator")
		self.declared = derived.register(
			derived.DerivedField(
				doctype=TASK,
				fieldname=self.CODE_FIELD,
				label="Code Probe",
				buckets=[
					derived.Bucket("Closed", [("status", "in", ["Done", "Canceled"])]),
					derived.Bucket("Open", [("status", "not in", ["Done", "Canceled"])]),
				],
			)
		)
		self.addCleanup(derived._REGISTRY.get(TASK, {}).pop, self.CODE_FIELD, None)
		self.addCleanup(derived.reload)

	def test_a_code_declaration_offers_every_menu_and_is_not_a_default_column(self):
		self.assertEqual(self.declared.surfaces, derived.SURFACES)
		self.assertEqual(self.declared.default_column, 0)

	def test_a_declaration_that_names_no_theme_describes_itself_as_it_always_did(self):
		self.assertNotIn("themes", self.declared.descriptor())


class TestSurfacesParsing(FrappeTestCase):
	def test_blank_means_every_menu(self):
		for blank in (None, "", "   ", "\n"):
			with self.subTest(repr(blank)):
				self.assertEqual(derived._surfaces(blank), derived.SURFACES)

	def test_a_row_may_separate_them_by_comma_or_newline_in_any_case(self):
		self.assertEqual(derived._surfaces("Filter, SORT"), ("filter", "sort"))
		self.assertEqual(derived._surfaces("column\ngroup_by\ncolumn"), ("column", "group_by"))

	def test_a_surface_no_menu_serves_is_refused(self):
		# Authored, saved and then offered nowhere is the silent failure this refusal exists to prevent.
		with self.assertRaises(derived.DerivedFieldError) as caught:
			derived.register(_probe("_probe_bad_surface", surfaces=("kanban",)))
		self.assertIn("kanban", str(caught.exception))


class TestSurfacesDecideWhichMenusOfferIt(FrappeTestCase):
	"""A declaration answers which menus offer it, and this module answers nothing."""

	MENUS: typing.ClassVar[dict] = {
		derived.FILTER: task_lenses.get_filterable_fields,
		derived.GROUP_BY: task_lenses.get_group_by_fields,
		derived.SORT: task_lenses.sort_options,
		derived.COLUMN: task_lenses.get_column_fields,
	}

	def test_a_field_declared_for_one_menu_appears_in_that_menu_alone(self):
		for surface in self.MENUS:
			with self.subTest(surface):
				with _registered(_probe("_probe_surface", surfaces=(surface,))) as field:
					for other, menu in self.MENUS.items():
						offered = [f.get("fieldname") for f in menu(TASK)]
						self.assertEqual(
							field.fieldname in offered,
							other == surface,
							f"declared for {surface}, {other} disagrees",
						)

	def test_the_bar_does_not_resolve_a_field_that_withholds_it(self):
		from tatva_connect.tests.list_engine.test_quick_filters import _store

		with _registered(_probe("_probe_bar", surfaces=(derived.FILTER,))) as field:
			_store(TASK, ["status", field.fieldname])
			self.addCleanup(frappe.clear_cache, doctype=TASK)
			offered = task_lenses.get_quick_filters(TASK, cached=False)
			self.assertNotIn(field.fieldname, [f.get("fieldname") for f in offered])

	def test_a_derived_name_is_still_withheld_from_the_property_setter_write(self):
		"""A Property Setter describing a column that does not exist is wrong whichever menu the name came
		from, so the write path withholds EVERY derived name, not the ones this surface offers."""
		from tatva_connect.tests.list_engine.test_quick_filters import _setters, _store

		with _registered(_probe("_probe_write", surfaces=(derived.FILTER,))) as field:
			_store(TASK, ["status"])
			self.addCleanup(frappe.clear_cache, doctype=TASK)
			task_lenses.update_quick_filters(
				json.dumps(["status", field.fieldname]), json.dumps(["status"]), TASK
			)
			self.assertNotIn(field.fieldname, [name for name, _ in _setters(TASK)])


class TestDefaultColumnIsShownWithoutBeingAdded(ListEngineCase):
	def test_a_default_column_reaches_a_caller_who_named_none(self):
		with _registered(_probe("_probe_default", default_column=1)) as field:
			result = self._get_data(rows=_rows_arg("name", "title"))
			column = next(
				(c for c in result["columns"] if c.get("key") == field.fieldname),
				None,
			)
			self.assertIsNotNone(column, "a declared default column never reached the answer")
			self.assertEqual(column["label"], field.descriptor()["label"])
			self.assertEqual(column["is_derived"], 1)
			self.assertTrue(all(row.get(field.fieldname) for row in result["data"]))

	def test_a_caller_who_named_columns_has_already_decided(self):
		asked = [{"label": "Title", "type": "Data", "key": "title", "width": "16rem"}]
		with _registered(_probe("_probe_named", default_column=1)) as field:
			result = self._get_data(columns=asked, rows=_rows_arg("name", "title"))
			self.assertEqual([c.get("key") for c in result["columns"]], ["title"])
			self.assertTrue(all(field.fieldname not in row for row in result["data"]))

	def test_a_field_that_is_not_a_default_column_is_still_only_available(self):
		# The floor: `due_state` declares nothing, so it must not appear until a rep adds it.
		result = self._get_data(rows=_rows_arg("name", "title", FIELD))
		self.assertNotIn(FIELD, [c.get("key") for c in result["columns"]])


class AuthoredRowCase(FrappeTestCase):
	"""Redis is not rolled back with the transaction, so every test here drops the cache on both sides."""

	def setUp(self):
		frappe.set_user("Administrator")
		# The row is cleared here, not only rolled back: an authoring test writes through `validate`, which
		# opens its own savepoint for `verify()`, so a leftover row from a sibling test collides on the name.
		frappe.db.delete(derived.ROW_DOCTYPE, {"fieldname": ["like", r"\_%"]})
		derived.reload()
		self.addCleanup(derived.reload)
		self.addCleanup(frappe.db.delete, derived.ROW_DOCTYPE, {"fieldname": ["like", r"\_%"]})


class TestTheRowIsTheSameCitizen(AuthoredRowCase):
	def test_an_enabled_row_is_served_by_the_registry(self):
		_authored_row()
		derived.reload()
		field = derived.get(TASK, "_probe_authored")
		self.assertIsNotNone(field, "an enabled row never reached the registry")
		self.assertEqual(field.options, ("Open", "Closed"))
		self.assertEqual(field.themes, {"Open": "green", "Closed": "gray"})

	def test_a_saved_row_is_live_with_no_reload_of_its_own(self):
		"""The whole promise. `on_update` drops the cache, so the row is live on Save — no deploy, no
		migrate, no worker restart. This drives the `hooks.py` wiring, not the function."""
		_authored_row()
		self.assertIn("_probe_authored", derived.names(TASK))

	def test_a_disabled_row_changes_nothing_anywhere(self):
		before = [f.fieldname for f in derived.for_doctype(TASK)]
		version = derived.declaration_version()
		_authored_row(enabled=0)
		derived.reload()
		self.assertEqual([f.fieldname for f in derived.for_doctype(TASK)], before)
		self.assertEqual(derived.declaration_version(), version)

	def test_an_enabled_row_reaches_the_menus_it_names(self):
		_authored_row(surfaces="filter")
		derived.reload()
		offered = [f.get("fieldname") for f in task_lenses.get_filterable_fields(TASK)]
		self.assertIn("_probe_authored", offered)
		self.assertNotIn("_probe_authored", [f.get("fieldname") for f in task_lenses.sort_options(TASK)])


class TestTheVersionAndTheCache(AuthoredRowCase):
	def test_the_version_is_stable_while_nothing_changes(self):
		# A version that churns would make a client rebuild every no-expiry cache it holds on every load.
		self.assertEqual(derived.declaration_version(), derived.declaration_version())
		derived.reload()
		self.assertEqual(derived.declaration_version(), derived.declaration_version())

	def test_the_version_changes_when_an_enabled_row_does(self):
		before = derived.declaration_version()
		_authored_row()
		derived.reload()
		self.assertNotEqual(derived.declaration_version(), before)

	def test_the_endpoint_hands_the_client_that_same_string(self):
		self.assertEqual(task_lenses.declaration_version(), derived.declaration_version())

	def test_the_rows_are_read_once_and_cached(self):
		"""No query per request. The first read fills Redis and this request; the second reads neither."""
		derived.reload()
		self.assertIsNone(frappe.cache().get_value(derived._CACHE_KEY))
		first = derived._rows()
		self.assertIsNotNone(frappe.cache().get_value(derived._CACHE_KEY))
		self.assertIs(derived._rows(), first, "the payload was rebuilt within one request")


class TestTheCollisionIsRefusable(FrappeTestCase):
	def test_a_code_declared_fieldname_is_reported_to_the_controller(self):
		"""A row shadowing a code declaration would be stored, enabled, and then silently never served —
		code wins the merge. `due_state` is a ROW now, so the code side is declared here to be asked about,
		and the answer must be about the CODE source alone: a seeded row must NOT read as code-declared."""
		probe = "_code_collision_probe"
		derived.register(
			derived.DerivedField(
				doctype=TASK,
				fieldname=probe,
				label="Collision Probe",
				buckets=[derived.Bucket("All", [("status", "is", "set")])],
			)
		)
		self.addCleanup(derived._REGISTRY.get(TASK, {}).pop, probe, None)
		self.assertTrue(derived.code_declared(TASK, probe))
		self.assertFalse(derived.code_declared(TASK, "_never_declared"))
		self.assertFalse(derived.code_declared("CRM Call Log", probe))
		self.assertFalse(derived.code_declared(TASK, FIELD), "a seeded ROW must not read as code-declared")
