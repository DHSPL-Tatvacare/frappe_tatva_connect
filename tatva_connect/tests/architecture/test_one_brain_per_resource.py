import ast, re, pathlib
import frappe
from frappe.tests.utils import FrappeTestCase

APP = pathlib.Path(frappe.get_app_path("tatva_connect"))

# The seven section keys. A literal collection enumerating >=3 of them IS a rival section registry:
# the keys' one home is the CRM Lead Section rows, reached through the `section` Link. No module may
# restate them as a dict/list/set/tuple literal — that is exactly the copy the brain replaced.
# section_seed.py (the seed that WRITES those rows) and the doctype dir are the home.
SECTION_KEYS = {"lead", "acq", "plan", "lab", "care", "drug", "metrics"}
SECTION_REGISTRY_EXEMPT = ("partner_api/section_seed.py", "partner_api/doctype/crm_lead_section/")


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
