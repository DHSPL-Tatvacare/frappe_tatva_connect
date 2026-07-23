"""Referential guard that keeps M2 true: nothing reads a file off the disk.

The bytes are in Azure. `FileOverride` is the ONE class that knows it, and it answers both questions a
caller can ask: `get_content()` for bytes, `get_full_path()` for a path that opens. Anything else that
reaches for a file's location on disk is reading a disk that no longer holds the file, and it breaks
silently: an empty import, a missing thumbnail, a blank image in a PDF.

That class of bug cost three months. This is the wall. A NEW disk-reading call site inside this app
fails `bench migrate`. The author either routes it through `FileOverride`, or reviews it and adds it to
`REVIEWED` below with the reason it is safe.

Frappe's next upgrade is the threat model, so the sweep reads the source rather than importing it: a
caller that only appears when some code path runs would never be caught by exercising the app.

Runs on after_migrate, beside the automation and notification drift checks.
"""
import ast
import pathlib

import frappe

# The reads that mean "give me a location on the local disk". `get_full_path` is deliberately NOT here:
# it is the sanctioned M2 door and FileOverride owns it, which is why `file_events` may call it to
# capture the local copy before the offload drops it.
BANNED = ("get_site_path", "get_local_image")

# Call sites reviewed and permitted, each with the reason it is safe. Keyed `relpath:function`, never a
# line number, so a comment added above a site does not read as a new violation.
# Empty, and verified empty: this app has no disk reads today. An entry here is a deliberate exception,
# never a convenience. If this set grows, M2 is eroding.
REVIEWED = {
	# This module writes its OWN run artifacts (a JSON per run) into a scratch directory and reads them
	# back from the same place. They are never `File` documents, so nothing offloads them and there is no
	# remote copy to be out of step with — the M2 threat model (a File's bytes moved, the reader still
	# opening a disk) does not apply here. The module is temporary; this entry goes when it does.
	"migration_check/storage.py:_root",
}

_SKIP_DIRS = {"tests", "__pycache__", "node_modules"}


def _app_root():
	"""The app directory, from frappe rather than from this file's position.

	`Path(__file__).parent.parent` would be positional: move this module one directory and the sweep
	scans the wrong tree, finds nothing, and the guard passes. A guard that fails OPEN is worse than no
	guard, so the root is asked of frappe, which knows where the app is installed.
	"""
	return pathlib.Path(frappe.get_app_path("tatva_connect"))


def _sites(tree, relpath):
	"""Every banned call in one module, as `relpath:enclosing_function`."""
	parents = {}
	for node in ast.walk(tree):
		for child in ast.iter_child_nodes(node):
			parents[child] = node

	found = set()
	for node in ast.walk(tree):
		if not isinstance(node, ast.Call):
			continue
		name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
		if name not in BANNED:
			continue
		# Walk up to the enclosing def, so the key survives an edit that moves the line.
		owner, cur = "<module>", node
		while cur in parents:
			cur = parents[cur]
			if isinstance(cur, ast.FunctionDef | ast.AsyncFunctionDef):
				owner = cur.name
				break
		found.add(f"{relpath}:{owner}")
	return found


def assert_no_disk_reads():
	"""Fail the migrate if a new call site reads a file by local path instead of asking FileOverride."""
	root = _app_root()
	offenders = set()
	for path in root.rglob("*.py"):
		if _SKIP_DIRS & set(path.relative_to(root).parts):
			continue
		try:
			tree = ast.parse(path.read_text())
		except SyntaxError:
			continue
		offenders |= _sites(tree, str(path.relative_to(root)))

	new = offenders - REVIEWED
	if new:
		listed = "\n  ".join(sorted(new))
		frappe.throw(
			"File-layer drift (M2): a file's bytes live in Azure, so these call sites read a disk that "
			"does not hold them:\n  " + listed + "\n\n"
			"Ask FileOverride instead: `get_content()` for bytes, `get_full_path()` for a path that "
			"opens. If the site is genuinely correct (it handles the local file BEFORE the offload), add "
			"it to REVIEWED in tatva_connect/storage/drift.py with the reason."
		)

	stale = REVIEWED - offenders
	if stale:
		frappe.throw(
			"File-layer drift: REVIEWED in tatva_connect/storage/drift.py names call site(s) that no "
			f"longer exist: {sorted(stale)}. Remove them, so the allowlist never grants more than it must."
		)
