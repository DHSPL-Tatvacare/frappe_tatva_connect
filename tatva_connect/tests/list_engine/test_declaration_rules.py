# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A bad declaration must fail the DAY IT IS WRITTEN, not on a rep's click — freeze list §16, items 16-18.

`derived.py` is a declaration primitive, so every rule it can state belongs in `_validate` (at `register`,
which runs at import) or in `_assert_declarable` (deferred, because it needs `frappe.get_meta`). The three
holes this file closes were all "accepted at import, wrong at runtime":

  * A SORT PROXY IS A COLUMN. `engine.for_native` joins `order_by` with the caller's own direction, so it
    is a BARE fieldname naming a real column — `"due_date asc"` builds `"due_date asc desc"`, and a proxy
    naming nothing at all reaches SQL as a column that does not exist.
  * A DECLARATION IS SORTABLE OR IT IS NOT, and the sort menu offers only the ones that are. Sort is the
    ONE lens where a derived name must resolve to something SQL can order by; filter, group-by and columns
    are answered by the declaration itself and still offer everything. Measured before this: sorting by a
    proxy-less field answered `PermissionError: You do not have permission to access field: CRM Task.due_state`.
  * `verify()` MAY NOT LIE BY OMISSION. It is the anti-drift spine (`derived.py:20-28`); an operand the
    column cannot hold used to be dropped silently, so a declaration could return clean having probed
    almost nothing.

Item 7 is RECORDED here rather than fixed: a real `due_state` column would take the whole Tasks list down.
Fail-loud is the decision — a shadowed field genuinely is unserveable — so what is pinned is that the
message names the blast radius for whoever reads the error log.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.list_engine.test_declaration_rules
"""

import typing

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.api import task_lenses
from tatva_connect.list_engine import derived, engine, fields

TASK = "CRM Task"
PROBE_DEFAULTS = {"title": "DeclarationProbe"}


def _field(fieldname, **overrides):
	"""A minimal sound declaration, so each test varies exactly the one thing it is about."""
	shape = {
		"doctype": TASK,
		"fieldname": fieldname,
		"label": "Probe",
		"buckets": [
			derived.Bucket("Closed", [("status", "in", fields.CLOSED)]),
			derived.Bucket("Open", [("status", "not in", fields.CLOSED)]),
		],
	}
	shape.update(overrides)
	return derived.DerivedField(**shape)


class DeclarationCase(FrappeTestCase):
	"""Every test here declares into the live registry, so every test takes its declaration back out."""

	def setUp(self):
		frappe.set_user("Administrator")
		self.declared = []

	def tearDown(self):
		for fieldname in self.declared:
			derived._REGISTRY.get(TASK, {}).pop(fieldname, None)
		derived._validated().discard(TASK)

	def _register(self, field):
		self.declared.append(field.fieldname)
		return derived.register(field)


class TestASortProxyIsARealColumn(DeclarationCase):
	"""Item #17. `_validate` checked buckets, uniqueness, arity and self-reference — and nothing at all
	about `order_by`, the one declared string that reaches SQL."""

	NOT_BARE: typing.ClassVar[dict] = {
		"a direction": "due_date asc",
		"two terms": "due_date desc, name asc",
		"quoted": "`due_date`",
		"empty": "",
		"not a string": 1,
	}

	def test_an_order_by_that_is_not_a_bare_fieldname_is_refused(self):
		for label, order_by in self.NOT_BARE.items():
			with self.subTest(label), self.assertRaises(derived.DerivedFieldError) as raised:
				self._register(_field("_probe_order_by", order_by=order_by))
			self.assertIn("order_by", str(raised.exception), label)
		# The control: the shape the engine actually joins a direction onto is accepted.
		self.assertTrue(self._register(_field("_probe_order_by", order_by="due_date")).sortable)

	def test_a_field_may_not_name_itself_as_its_own_sort_proxy(self):
		with self.assertRaises(derived.DerivedFieldError):
			self._register(_field("_probe_self_sort", order_by="_probe_self_sort"))

	def test_a_proxy_that_names_no_column_is_refused_where_meta_is_first_available(self):
		"""Whether a column exists is not knowable at import — `_assert_declarable` defers exactly this
		question, so the proxy is checked on the same meta, in the same one-shot pass."""
		self._register(_field("_probe_ghost_proxy", order_by="not_a_column"))
		with self.assertRaises(derived.DerivedFieldError) as raised:
			derived.for_doctype(TASK)
		self.assertIn("not_a_column", str(raised.exception))

		# The control: `modified` is a real indexed column `meta.get_field` does not answer for, and the
		# obvious proxy for half the fields anyone will declare — the rule may not refuse it.
		derived._REGISTRY.get(TASK, {}).pop("_probe_ghost_proxy", None)
		self._register(_field("_probe_std_proxy", order_by="modified"))
		self.assertEqual(len(derived.for_doctype(TASK)), len(derived.names(TASK)))


class TestOnlyASortableDeclarationReachesTheSortMenu(DeclarationCase):
	"""Item #16. `lens_fields()` filters on nothing and feeds all four menus, so a declaration with no proxy
	was offered in Sort — where the term is then dropped and the rep's chosen order silently does nothing.
	The rule is the declaration's; the menu is the only place it is applied."""

	def test_a_declaration_with_no_proxy_is_offered_by_every_menu_except_sort(self):
		probe = self._register(_field("_probe_unsortable"))
		self.assertFalse(probe.sortable)
		for menu, offered in {
			"filter": task_lenses.get_filterable_fields(TASK),
			"group_by": task_lenses.get_group_by_fields(TASK),
			"columns": task_lenses.get_column_fields(TASK),
		}.items():
			with self.subTest(menu):
				self.assertIn(
					probe.fieldname,
					[f.get("fieldname") for f in offered],
					f"{menu} must still offer a field the declaration answers for",
				)

		sortable = [f.get("fieldname") for f in task_lenses.sort_options(TASK)]
		self.assertNotIn(probe.fieldname, sortable, "sort offers a field SQL has nothing to order by")
		self.assertIn(fields.DUE_STATE.fieldname, sortable, "a declaration that HAS a proxy is still offered")

		# THE ONE RULE: a doctype that declares nothing is answered by native on its own arguments.
		from crm.api.doc import sort_options as native

		self.assertEqual(
			frappe.as_json(task_lenses.sort_options("CRM Lead")), frappe.as_json(native("CRM Lead"))
		)


class TestTheVerifierCannotPassVacuously(DeclarationCase):
	"""Item #18. `_probe_values` kept only the literals that survive `_as_datetime`, which swallows both
	`ValueError` and `TypeError` — so a `Timespan` operand was dropped, no `Disagreement` was recorded, and
	`verify()` returned clean having put a row on none of the boundaries that operand names."""

	def test_an_operand_the_column_cannot_hold_is_reported_never_skipped(self):
		field = derived.DerivedField(
			doctype=TASK,
			fieldname="_probe_timespan",
			label="Probe",
			buckets=[
				derived.Bucket("Recent", [("due_date", "is", "set"), ("due_date", "timespan", "last week")]),
				derived.Bucket("No Due Date", [("due_date", "is", "not set")]),
			],
		)
		problems = derived.verify(field, defaults=PROBE_DEFAULTS)
		holes = [p for p in problems if p.kind == "operand-unrepresentable"]
		self.assertTrue(holes, f"verify() passed having never probed the timespan: {problems}")
		for hole in holes:
			# `set` / `not set` name the empty case the corpus always probes with None; they are not holes.
			self.assertIn("last week", hole.detail)
			self.assertIn("due_date", hole.detail)


class TestAShadowedDeclarationIsUnserveableAndSaysSo(DeclarationCase):
	"""Item #7, RECORDED not changed. `_assert_declarable` raises from `for_doctype`, which `get_data` calls
	on EVERY `CRM Task` request — so the day a real column with that fieldname lands, the whole Tasks list
	500s for everyone rather than degrading. That is deliberate: the cell and the column would otherwise
	disagree in silence, which is the one outcome this layer exists to prevent. No fallback is added; what
	is pinned is that the error an operator reads names the blast radius and how to end it."""

	def test_the_hot_read_path_fails_loud_and_the_message_names_the_blast_radius(self):
		self._register(_field("status", buckets=[derived.Bucket("A", [("due_date", "is", "set")])]))
		with self.assertRaises(derived.DerivedFieldError) as raised:
			engine.get_data(doctype=TASK, filters={}, page_length=1)
		message = str(raised.exception)
		self.assertIn("CRM Task.status", message)
		self.assertIn("unserveable", message)
