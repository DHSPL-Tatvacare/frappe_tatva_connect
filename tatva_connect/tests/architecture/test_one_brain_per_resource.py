import ast
import pathlib
import re

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.taxonomy import task_section_seed

APP = pathlib.Path(frappe.get_app_path("tatva_connect"))

# The section keys. A literal collection enumerating >=3 of them IS a rival section registry:
# the keys' one home is the CRM Lead Section rows, reached through the `section` Link. No module may
# restate them as a dict/list/set/tuple literal — that is exactly the copy the brain replaced.
# section_seed.py (the seed that WRITES those rows) and the doctype dir are the home.
SECTION_KEYS = {"lead", "acq", "plan", "lab", "screening", "care", "drug", "metrics"}
SECTION_REGISTRY_EXEMPT = ("partner_api/section_seed.py", "partner_api/doctype/crm_lead_section/")


# The SAME rule for activity sections, read off the seed that writes those rows rather than restated —
# a list here would be the very copy this file exists to forbid. Two things are locked, for two reasons:
#
#   STORAGE (a section's target doctype and the CRM Task Table field holding its rows) may not be named
#   as a literal at all. Phase 2 is the first phase with a writer that could reach for one, and reaching
#   for one is exactly how a writer stops asking the declaration where a value goes.
#
#   A SECTION KEY may not be compared against, nor collected with its siblings. It is not banned outright
#   because two of the four keys are ordinary English words already used as payload dict keys in unrelated
#   live code (`{"documents": ...}`, `x["order"]`), so an outright ban would be red for reasons that are
#   no defect. Addressing a section by literal still cannot survive: the table it names cannot be spelled.
#
# patches/ is exempt on the plan's own rule (§11): a migration is allowed to know history, and the index
# patches must name the table they index.
TASK_SECTION_KEYS = {r["section_key"] for r in task_section_seed._ROWS}
TASK_STORAGE_NAMES = ({r["target_doctype"] for r in task_section_seed._ROWS}
					  | {r["child_table_field"] for r in task_section_seed._ROWS}) - {""}
TASK_SECTION_EXEMPT = ("taxonomy/task_section_seed.py", "patches/")


def _string_elements(node):
	"""The str constant values directly held by a Dict/List/Set/Tuple literal (keys for a Dict)."""
	if isinstance(node, ast.Dict):
		items = node.keys
	else:
		items = node.elts
	return {e.value for e in items if isinstance(e, ast.Constant) and isinstance(e.value, str)}

# C5 for Leads: these retired-copy symbols must not exist anywhere in app source (non-test).
# Each symbol must be UNIQUE to its rival — a generic name (e.g. `_TABLE`, used as a DDL constant in
# several patches) is not a valid sentinel and would keep this gate RED forever. The intake `_TABLE`
# dict and every hardcoded `custom_*_profile` literal are gated SEMANTICALLY (AST) in their own phases
# (intake -> Phase 2, child-table literals -> Phase 7), not by name here.
FORBIDDEN_LEAD_SYMBOLS = {"SECTION_REGISTRY"}


class TestNoLeadSecondaryPath(FrappeTestCase):
    def test_no_retired_symbol_survives_in_source(self):
        hits = []
        for py in APP.rglob("*.py"):
            # live source only: test code and parked dead code (.archive) are not a live path.
            if "/tests/" in str(py) or "/.archive/" in str(py):
                continue
            src = py.read_text()
            for sym in FORBIDDEN_LEAD_SYMBOLS:
                if re.search(rf"\b{re.escape(sym)}\b", src):
                    hits.append(f"{py.relative_to(APP)}: {sym}")
        self.assertEqual(hits, [], f"retired copy symbols still present: {hits}")

    def test_copy_columns_absent_from_catalog_meta(self):
        fields = {f.fieldname for f in frappe.get_meta("CRM Lead API Field").fields}
        copies = {"section_key", "target_doctype", "child_table_field", "child_pick"}
        self.assertEqual(fields & copies, set(), "copy columns still on the catalog")

    def test_no_rival_section_registry_in_source(self):
        hits = []
        for py in APP.rglob("*.py"):
            rel = str(py.relative_to(APP))
            if "/tests/" in str(py) or "/.archive/" in str(py):
                continue
            if any(x in rel for x in SECTION_REGISTRY_EXEMPT):
                continue
            tree = ast.parse(py.read_text())
            for node in ast.walk(tree):
                if isinstance(node, (ast.Dict, ast.List, ast.Set, ast.Tuple)):
                    if len(_string_elements(node) & SECTION_KEYS) >= 3:
                        hits.append(f"{rel}:{node.lineno}")
        self.assertEqual(hits, [], f"rival section-key registry (literal): {hits}")


def _task_source_files():
    """Live app source subject to the task-section rule — tests, parked code and patches excepted."""
    for py in APP.rglob("*.py"):
        rel = str(py.relative_to(APP))
        if "/tests/" in str(py) or "/.archive/" in str(py):
            continue
        if any(x in rel for x in TASK_SECTION_EXEMPT):
            continue
        yield rel, ast.parse(py.read_text())


class TestNoTaskSectionSecondaryPath(FrappeTestCase):
    """An activity field's home is the CRM Task Section row that declares it, reached through the field's
    own `section` Link — never a name a module happens to know."""

    def test_no_task_storage_name_is_named_as_a_literal(self):
        """The child doctype and the Table field holding its rows are the seed's to name, and only its."""
        hits = []
        for rel, tree in _task_source_files():
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and node.value in TASK_STORAGE_NAMES:
                    hits.append(f"{rel}:{node.lineno}: {node.value!r}")
        self.assertEqual(hits, [], f"task section storage named as a literal: {hits}")

    def test_no_task_section_key_is_compared_against_or_collected(self):
        """A key tested by name, or listed beside its siblings, is a routing decision taken outside the
        declaration — which is the whole of what the section rows exist to hold."""
        hits = []
        for rel, tree in _task_source_files():
            for node in ast.walk(tree):
                if isinstance(node, ast.Compare):
                    operands = [node.left, *node.comparators]
                    for o in operands:
                        if isinstance(o, ast.Constant) and o.value in TASK_SECTION_KEYS:
                            hits.append(f"{rel}:{o.lineno}: compared against {o.value!r}")
                if isinstance(node, (ast.Dict, ast.List, ast.Set, ast.Tuple)):
                    named = _string_elements(node) & TASK_SECTION_KEYS
                    if len(named) >= 2:
                        hits.append(f"{rel}:{node.lineno}: literal naming {sorted(named)}")
        self.assertEqual(hits, [], f"task section key decided in code: {hits}")
