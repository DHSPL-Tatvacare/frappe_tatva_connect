# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Shared AST plumbing for the marker-gated static locks (constitution A.8: one brain, not
two) — `test_no_perm_bypass.py` (the `# authz-ok:` bypass lock) and `test_guest_endpoints.py`
(the `# guest-ok:` public-endpoint lock). Both scan source with `ast`, both read a marker
COMMENT from raw source text (never from the AST — a comment isn't a node), and both derive
"does this function's own code mention a guard/gate token" the same way.

Not a test itself: no `test_*` filename, so it is never collected by pytest and never picked
up by the tcsec `locks` auto-discovery (`_static_lock_files()` in `security/tcsec.py`).

Pure stdlib — runs standalone (tcsec, no frappe) and inside `bench run-tests`.
"""
import ast
import os


def app_root(start_file):
	"""The tatva_connect app dir, found by walking up from `start_file` to the `hooks.py`
	marker — robust to where the calling test file sits, identical in the bench and standalone."""
	d = os.path.dirname(os.path.abspath(start_file))
	while d != os.path.dirname(d):
		if os.path.exists(os.path.join(d, "hooks.py")):
			return d
		d = os.path.dirname(d)
	raise RuntimeError("tatva_connect app root (hooks.py) not found above this test")


def is_whitelist_decorator(decorator):
	"""True for `@frappe.whitelist` or `@frappe.whitelist(...)` (any kwargs, any truth value)."""
	node = decorator.func if isinstance(decorator, ast.Call) else decorator
	return isinstance(node, ast.Attribute) and node.attr == "whitelist"


def guard_text(node):
	"""A function's CODE as text for a guard/gate-token membership check — decorators +
	executable statements, with any leading docstring dropped. Built from the AST
	(`ast.unparse`), so docstrings and `#` comments NEVER contribute a token: a function whose
	only mention of a guard name is in its docstring or a comment is NOT cleared."""
	parts = [ast.unparse(d) for d in node.decorator_list]
	stmts = node.body
	if (
		stmts
		and isinstance(stmts[0], ast.Expr)
		and isinstance(stmts[0].value, ast.Constant)
		and isinstance(stmts[0].value.value, str)
	):
		stmts = stmts[1:]  # drop the docstring
	parts.extend(ast.unparse(s) for s in stmts)
	return "\n".join(parts)


def span_text(lines, lineno, end_lineno):
	"""Raw source text (1-indexed, inclusive) for a node's line span — used to look for an
	at-code `# ...-ok:` marker near a decorator/call. Distinct from `guard_text`'s AST-derived,
	code-only view: a marker is a COMMENT, so it can only be found in raw text, never the AST."""
	return "\n".join(lines[lineno - 1 : end_lineno])
