# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Gate: every dotted path that names a `tatva_connect` function must resolve to one that exists.

THE BLIND SPOT THIS CLOSES. Frappe calls Python by STRING — from the SPA (`call('tatva_connect...')`),
from `hooks.py`, from `patches.txt`, from doctype JSON. None of those is an import, so no linter, no
type checker and no unit test follows them. Worse, the SPA lives in a DIFFERENT REPO: rename a function
here, grep this repo for callers, find none, and ship a break you cannot see.

Three real breaks on one night proved it, each invisible to a green suite of 189 tests:

  * `whatsapp.routing.lead_has_route` lost its `@frappe.whitelist()` — a delete of the function ABOVE it
    overshot by one line and took the decorator. The file still parsed, Python callers still worked, and
    only the BROWSER was refused.
  * `api.whatsapp.whatsapp_window_state` was deleted outright while two Vue components still called it.
  * `templates_sync.sync_from_wati` was renamed to `sync_templates`; `hooks.py` was updated and the
    frontend was not.

Every unit test calls these functions DIRECTLY as Python, where a missing decorator and a name in
another repo's string both mean nothing. That is why the suite stayed green.

TWO CLASSES, because they have different contracts:
  * BROWSER-CALLABLE (the SPA) — must exist AND carry `@frappe.whitelist()`.
  * SERVER-INTERNAL (hooks, patches, doctype JSON) — must exist. Whitelisting is irrelevant and a
    whitelist here would be a defect of its own.

Static only: paths are parsed out of source with `ast`/regex and resolved against the module tree. No
DB, no site, no import of the target (importing would run module-level code and could mask a failure).
"""
import ast
import re
import unittest
from pathlib import Path

import frappe

_APP = Path(frappe.get_app_path("tatva_connect"))
_APP_ROOT = _APP.parent

# A dotted path inside a QUOTED string. Quotes matter: prose in a comment that happens to name a
# function is documentation drift, not a broken call, and must not fail this gate.
_QUOTED = re.compile(r"""['"](tatva_connect\.[a-zA-Z_][a-zA-Z0-9_.]*)['"]""")


def _crm_frontend() -> Path | None:
	"""The fork's SPA source. On a bench it is apps/crm; in a bare checkout, a sibling repo."""
	try:
		# get_app_path returns the MODULE dir (apps/crm/crm); the SPA sits beside it at apps/crm/frontend.
		candidate = Path(frappe.get_app_path("crm")).parent / "frontend" / "src"
		if candidate.is_dir():
			return candidate
	except Exception:
		pass
	sibling = _APP_ROOT.parent / "frappe_tatva_crm" / "frontend" / "src"
	return sibling if sibling.is_dir() else None


def _resolve(dotted: str):
	"""(path, function_name) for a dotted path, or None when it names no function in this app.

	Walks right-to-left so `a.b.c.d` tries `a/b/c.py::d` before `a/b.py::c`. A path that resolves to a
	MODULE and no function is not a call target — a resource url prefix, a module reference — and is
	skipped rather than failed.
	"""
	parts = dotted.split(".")
	for split in range(len(parts) - 1, 1, -1):
		module = _APP_ROOT.joinpath(*parts[:split]).with_suffix(".py")
		if module.is_file():
			return module, parts[split]
	return None


def _defined(path: Path, name: str):
	"""The ast node defining `name` at MODULE level in `path`, or None. Never imports the module.

	A dotted path can name a function, a CLASS (`override_doctype_class` points at one) or a plain
	module-level value (`jinja_methods` is a list). All three are legitimate targets, so all three
	count as defined — anything narrower reports a false break, which is worse than no gate at all.
	"""
	try:
		tree = ast.parse(path.read_text())
	except SyntaxError:
		return None
	for node in tree.body:
		if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name:
			return node
		if isinstance(node, ast.Assign):
			for target in node.targets:
				if isinstance(target, ast.Name) and target.id == name:
					return node
		if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name:
			return node
	return None


def _is_whitelisted(node) -> bool:
	"""Only a def can carry a decorator; anything else is not browser-callable by definition."""
	if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
		return False
	for deco in node.decorator_list:
		if "whitelist" in ast.dump(deco):
			return True
	return False


def _strip_comments(text: str, suffix: str) -> str:
	"""Drop commented-out lines before scanning.

	Frappe's generated hooks.py ships its whole optional surface commented out, quoted paths and all
	(`# before_app_install = "tatva_connect.utils.before_app_install"`). Those name functions nobody
	wrote and nobody calls — failing on them would make this gate cry wolf on every new app.
	"""
	if suffix == ".py":
		return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
	if suffix in (".vue", ".js", ".ts"):
		return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith(("//", "*", "/*")))
	return text


def _paths_in(files) -> set:
	found = set()
	for f in files:
		try:
			found.update(_QUOTED.findall(_strip_comments(f.read_text(), f.suffix)))
		except (UnicodeDecodeError, OSError):
			continue
	return found


class TestDottedPathTargets(unittest.TestCase):
	# --- browser-callable: must exist AND be whitelisted -------------------------------------------

	def test_every_frontend_call_target_exists_and_is_whitelisted(self):
		"""The SPA calls Python by string, from another repo. Nothing else checks these."""
		src = _crm_frontend()
		self.assertIsNotNone(
			src,
			"crm frontend source not found. This gate must never skip quietly — a skip here reads as a "
			"pass and is how the breaks it exists to catch got shipped.",
		)

		files = [p for p in src.rglob("*") if p.suffix in (".vue", ".js", ".ts")]
		self.assertTrue(files, "found no SPA source files — the glob is wrong, not the code")

		missing, unexposed = [], []
		for dotted in sorted(_paths_in(files)):
			resolved = _resolve(dotted)
			if resolved is None:
				continue
			path, name = resolved
			node = _defined(path, name)
			if node is None:
				missing.append(f"{dotted}  ->  {path.relative_to(_APP_ROOT)} does not define {name}")
			elif not _is_whitelisted(node):
				unexposed.append(f"{dotted}  ->  exists but carries no @frappe.whitelist()")

		self.assertEqual(missing, [], "SPA calls a function this app does not have:\n  " + "\n  ".join(missing))
		self.assertEqual(unexposed, [], "SPA calls a function the browser may not reach:\n  " + "\n  ".join(unexposed))

	# --- server-internal: must exist; whitelisting is NOT expected ---------------------------------

	def test_every_hooks_dotted_path_exists(self):
		"""doc_events, scheduler_events, overrides — all resolved by string at runtime."""
		self._assert_exist(_paths_in([_APP / "hooks.py"]), "hooks.py")

	def test_every_doctype_json_dotted_path_exists(self):
		"""A doctype JSON can name a Python path (e.g. a dashboard or a link builder)."""
		self._assert_exist(_paths_in(list(_APP.rglob("*.json"))), "doctype/fixture JSON")

	def test_every_patch_module_exists_and_has_execute(self):
		"""patches.txt names modules that must exist and expose execute(). A typo here fails a MIGRATE
		— on a customer site, mid-deploy — which is the most expensive place to find it."""
		txt = (_APP / "patches.txt").read_text()
		missing = []
		for raw in txt.splitlines():
			line = raw.split("#", 1)[0].strip()
			if not line or line.startswith("["):
				continue
			module = _APP_ROOT.joinpath(*line.split(".")).with_suffix(".py")
			if not module.is_file():
				missing.append(f"{line}  ->  no such module")
			elif _defined(module, "execute") is None:
				missing.append(f"{line}  ->  module has no execute()")
		self.assertEqual(missing, [], "patches.txt names a patch that cannot run:\n  " + "\n  ".join(missing))

	def _assert_exist(self, dotted_paths, where):
		missing = []
		for dotted in sorted(dotted_paths):
			resolved = _resolve(dotted)
			if resolved is None:
				continue
			path, name = resolved
			if _defined(path, name) is None:
				missing.append(f"{dotted}  ->  {path.relative_to(_APP_ROOT)} does not define {name}")
		self.assertEqual(missing, [], f"{where} names a function this app does not have:\n  " + "\n  ".join(missing))

	# --- the gate must bite -----------------------------------------------------------------------

	def test_the_gate_catches_a_planted_break(self):
		"""A gate nobody has seen fail is a gate nobody knows works."""
		self.assertIsNone(_resolve("tatva_connect.whatsapp.does_not_exist_at_all"))
		resolved = _resolve("tatva_connect.whatsapp.routing.lead_has_route")
		self.assertIsNotNone(resolved, "the resolver cannot find a function that definitely exists")
		self.assertIsNone(_defined(resolved[0], "no_such_function_planted_by_this_test"))
