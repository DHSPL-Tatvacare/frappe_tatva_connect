# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Every paged partner list ends on a unique sort key, so its pages are disjoint and complete.

Six endpoints paged over `modified desc` / `creation desc` and nothing else. Neither column is unique, so
tied rows had no defined order and LIMIT/OFFSET could read one on two pages or on none — while `total`
stayed right and the rows handed back were wrong. Same defect and same leaf as
tests/smartview/test_paging_is_disjoint.py.

The lock DISCOVERS the endpoints rather than naming them: anything calling `_page` is a paged list and
must order through the leaf, so a seventh one that forgets goes RED with nobody editing this file.

Run:
	bench --site dev.localhost run-tests --app tatva_connect \\
		--module tatva_connect.tests.api.test_paged_lists_end_on_a_unique_leaf
"""
import ast
import inspect
import unittest

import frappe

from tatva_connect.api import (
	_base,
	partner,
	partner_activity,
	partner_bulk_job,
	partner_call,
	partner_file,
	partner_note,
)

# Every module serving a paged partner list; the count below is what makes an unlisted one visible.
_MODULES = (partner, partner_activity, partner_bulk_job, partner_call, partner_file, partner_note)
_PAGED = 6

PAGE = 20
PAGES = 6


def _calls(node):
	"""Every function name called anywhere inside `node`, plus every attribute called on something."""
	names = set()
	for child in ast.walk(node):
		if not isinstance(child, ast.Call):
			continue
		fn = child.func
		if isinstance(fn, ast.Name):
			names.add(fn.id)
		elif isinstance(fn, ast.Attribute):
			names.add(fn.attr)
	return names


def _mentions(node, name):
	"""True if `name` appears as a bare name or an attribute anywhere inside `node`."""
	return any(
		(isinstance(n, ast.Name) and n.id == name) or (isinstance(n, ast.Attribute) and n.attr == name)
		for n in ast.walk(node)
	)


def _paged_functions():
	"""(module, function node) for every partner function that pages — read from the source, never from a
	list of names: what makes a function a paged list is that it cuts rows with our clamped limit/offset."""
	found = []
	for module in _MODULES:
		tree = ast.parse(inspect.getsource(module))
		for node in ast.walk(tree):
			if isinstance(node, ast.FunctionDef) and "_page" in _calls(node):
				found.append((module.__name__, node))
	return found


class TestTheOrderStringCarriesTheLeaf(unittest.TestCase):
	"""`_order_by` is the one brain; this pins what it emits, in both directions."""

	def test_a_descending_column_is_followed_by_the_leaf(self):
		self.assertEqual(_base._order_by("modified"), "modified desc, name asc")

	def test_an_ascending_column_is_followed_by_the_leaf(self):
		self.assertEqual(_base._order_by("record_index", "asc"), "record_index asc, name asc")

	def test_the_leaf_is_the_primary_key(self):
		"""`name` and nothing else: it is the only column Frappe guarantees unique on every doctype."""
		self.assertEqual(_base.SORT_LEAF, "name")


class TestEveryPagedEndpointOrdersThroughTheLeaf(unittest.TestCase):
	def test_every_paged_endpoint_orders_through_the_leaf(self):
		"""DRIFT LOCK. A function that pages must order on the leaf — via `_order_by` for a `get_all`, or on
		`SORT_LEAF` for the one endpoint paging through qb. The evasion: a new list endpoint copies its
		neighbour and hand-types `order_by="creation desc"` again. No allowlist to forget to update."""
		paged = _paged_functions()
		self.assertEqual(
			len(paged), _PAGED,
			f"expected {_PAGED} paged partner endpoints, found {len(paged)}: "
			f"{[f'{m}.{n.name}' for m, n in paged]}. A new one must order through the leaf too.",
		)
		for module_name, node in paged:
			with self.subTest(endpoint=f"{module_name}.{node.name}"):
				self.assertTrue(
					"_order_by" in _calls(node) or _mentions(node, "SORT_LEAF"),
					f"{module_name}.{node.name} pages with LIMIT/OFFSET but does not end its order on "
					f"`{_base.SORT_LEAF}`. Tied rows have no defined order, so a page can repeat a row "
					f"or drop one. Pass `order_by=_order_by('<column>')`.",
				)


class TestLeadListPagesAreDisjoint(unittest.TestCase):
	"""The behavioural half — the walk tests/smartview/test_paging_is_disjoint.py makes, at the partner door."""

	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")

	def setUp(self):
		self._form = frappe.form_dict
		frappe.local.response = frappe._dict()

	def tearDown(self):
		frappe.form_dict = self._form

	def _walk(self):
		names = []
		for page in range(PAGES):
			frappe.local.response = frappe._dict()
			frappe.form_dict = frappe._dict({"limit": PAGE, "offset": page * PAGE})
			partner.lead_list()
			rows = frappe.local.response["data"]["leads"]
			names += [r["name"] for r in rows]
			if len(rows) < PAGE:
				break
		return names

	def test_the_sort_column_really_does_tie(self):
		"""The precondition: without ties there is nothing for a leaf to decide, so it is stated not assumed."""
		tied = frappe.db.sql(
			"select count(*) from (select modified from `tabCRM Lead` group by modified having count(*) > 1) t"
		)[0][0]
		if not tied:
			self.skipTest("no two leads on this bench share a `modified`")
		self.assertGreater(tied, 0)

	def test_pages_do_not_repeat_a_row(self):
		"""RED without the leaf whenever a tie straddles a page boundary."""
		if frappe.db.count("CRM Lead") <= PAGE * 2:
			self.skipTest("this bench holds fewer than two pages of leads")
		names = self._walk()
		self.assertEqual(len(names), len(set(names)), "a lead was read on more than one page")

	def test_the_walk_is_stable_across_repeats(self):
		"""Two identical walks agree. Without the leaf the database may order a tie group differently each time."""
		if frappe.db.count("CRM Lead") <= PAGE * 2:
			self.skipTest("this bench holds fewer than two pages of leads")
		self.assertEqual(self._walk(), self._walk(), "two identical walks returned different rows")
