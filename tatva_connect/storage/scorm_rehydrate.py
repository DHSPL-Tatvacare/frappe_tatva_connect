# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A SCORM package's extracted tree is a CACHE of a File, not storage — rebuilt when the disk loses it.

The upload is one blob with a File row (M1), but the bytes a learner reads are the UNZIPPED tree under
`private/scorm/<course>/<title>/`, which has no row and dies with the container that held it. Nothing new is
recorded to fix that: `Course Chapter.scorm_package` already names the File, so the disk becomes what it
already is for `get_full_path()` — a cache with a rebuild rule (M2).

WRAPPED, NOT REPLACED, and only when the tree is ABSENT — a miss inside a package that IS extracted is a real
404 (a broken relative link), never a reason to re-download. LMS's own `extract_package` does the extraction,
so the traversal and symlink rules stay in one place. Permission is untouched: LMS's `render` runs
`_check_permission()` first, so a rebuild is only ever reached by a caller already allowed to read it.
"""
import os
from urllib.parse import unquote

import frappe
from frappe.utils.synchronization import filelock

_original = None


def install(*_args, **_kwargs):
	"""before_request: wrap the SCORM renderer once per process. No-op without the app."""
	global _original

	if _original is not None:
		return
	try:
		from lms.page_renderers import SCORMRenderer
	except ImportError:
		return

	_original = SCORMRenderer.render
	SCORMRenderer.render = _render


def _render(self):
	"""LMS answers None when no disk root holds the file — rebuild from the blob and let LMS answer again."""
	response = _original(self)
	if response is not None:
		return response
	if not _rebuild(self.path):
		return None
	return _original(self)


def _rebuild(path: str) -> bool:
	"""Re-extract this chapter's package from the File it was uploaded as. Never raises — on failure LMS's own miss stands."""
	from lms.lms.api import _scorm_extract_path, extract_package

	parts = path.strip("/").split("/")
	if len(parts) < 3 or parts[0] != "scorm":
		return False
	course, title = unquote(parts[1]), unquote(parts[2])

	try:
		extract_path = _scorm_extract_path(course, title)
	except Exception:
		return False  # LMS refuses the course/title pair on its own traversal rule; not ours to reinterpret

	# Only an ABSENT tree is a reason to re-download: a miss inside a package that is present is a real 404 (a broken relative link), and rebuilding on those would pull the zip on every such request.
	if os.path.isdir(extract_path):
		return False

	chapter = frappe.db.get_value(
		"Course Chapter",
		{"course": course, "title": title, "is_scorm_package": 1},
		["name", "scorm_package"],
		as_dict=True,
	)
	if not chapter or not chapter.scorm_package:
		return False
	if not frappe.db.exists("File", chapter.scorm_package):
		return False

	try:
		# `extract_package` rmtree's before it writes, so two concurrent misses would delete a tree the other is serving; the re-check inside the lock stops the queued request rebuilding what the winner just built.
		with filelock(f"scorm_extract_{frappe.scrub(chapter.name)}", timeout=60):
			if os.path.isdir(extract_path):
				return True
			extract_package(course, title, frappe._dict({"name": chapter.scorm_package}))
	except Exception:
		frappe.log_error(title="SCORM re-extract from blob failed")
		return False
	return True
