# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The dashboard has ONE query door, and this is what keeps it that way.

`frappe.get_list` applies the row gate: the `permission_query_conditions` hooks, every `User Permission`
row on every Link column, the owner constraint and shares, all ANDed before a row is counted. Nothing
else does. `frappe.get_all` sets `ignore_permissions=True` unconditionally (`frappe/__init__.py:1386`),
and `frappe.qb` builds its own SQL and inherits nothing — a chart written through either would be a
number computed over records the person looking at it may not see, and it would look exactly like a
correct chart.

THIS LOCK EXISTS BECAUSE THE TEMPTATION IS REAL AND ARRIVES SOON. `frappe.qb` is more capable than
`get_list`, and the first request for a chart the declaration cannot express will read as a reason to
reach for it. It is not one: the correct answer is that the chart is out of baseline, never a second
query path beside the gated one.

AST, never grep — a docstring naming a banned call (this one does, repeatedly) must not trip it.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.architecture.test_dashboard_has_one_query_door
"""

import ast
import pathlib

import frappe
from frappe.tests.utils import FrappeTestCase

_APP = pathlib.Path(frappe.get_app_path("tatva_connect"))

# The feature is the package AND its two doctype controllers: a read written in a controller is the same
# read, and the lock claimed "no exceptions" while the controllers sat outside it.
_MODULE = (
	_APP / "dashboard",
	_APP / "tatva_connect" / "doctype" / "crm_dashboard_chart",
	_APP / "tatva_connect" / "doctype" / "crm_dashboard_layout",
)

# There is no allowlist. `grain_charts.py` and `team_charts.py` were the only two files that ever needed
# one and they were deleted in Phase 2, Sequence 8 — this lock now covers the module with no exceptions,
# and that is the proof the retirement is complete. Do not add one back.

# `frappe.db.sql` and `frappe.db.sql_ddl`: raw SQL is not available to new code in this app at all.
_BANNED_DB = {"sql", "sql_ddl"}


def _hits(tree):
	"""Every banned call in one module, as (lineno, what). One walk, three shapes."""
	found = []
	for node in ast.walk(tree):
		if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
			continue
		func = node.func
		# frappe.qb.from_(...) — a query built here inherits none of the framework's gate.
		if func.attr == "from_" and isinstance(func.value, ast.Attribute) and func.value.attr == "qb":
			found.append((node.lineno, "frappe.qb.from_"))
		elif func.attr in _BANNED_DB and isinstance(func.value, ast.Attribute) and func.value.attr == "db":
			found.append((node.lineno, f"frappe.db.{func.attr}"))
		elif func.attr == "get_all" and isinstance(func.value, ast.Name) and func.value.id == "frappe":
			found.append((node.lineno, "frappe.get_all"))
	return found


class TestTheDashboardOnlyEverAsksGetList(FrappeTestCase):
	def test_no_dashboard_module_builds_its_own_query(self):
		hits = []
		for path in sorted(p for root in _MODULE for p in root.rglob("*.py")):
			if "tests" in path.parts:
				continue
			for lineno, what in _hits(ast.parse(path.read_text())):
				hits.append(f"{path.relative_to(_MODULE.parent)}:{lineno}: {what}")
		self.assertEqual(
			hits,
			[],
			"the dashboard bypassed its one gated query door — every chart is a frappe.get_list "
			f"call and nothing else: {hits}",
		)
