# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""The ONE door for force-reimporting a desk JSON — and the guard that keeps the file alive.

WHY A FORCE REIMPORT EXISTS. `import_file.py:141` skips a standard JSON whose DB row looks newer than the
file, so on any site whose desk was ever opened a correct edit migrates SILENTLY GREEN and changes
nothing. `force=True` bypasses that check. This has been needed fourteen times; the rule is in CLAUDE.md.

WHY IT NEEDS A GUARD. `force=True` does not update the row, it DELETES and re-inserts it — and on a
developer_mode site that deletes the exported file too:

    import_file_by_path(force=True)      sets frappe.flags.in_import = True      import_file.py:211
      -> delete_old_doc()                delete_doc(..., for_reload=True)        import_file.py:273
        -> doc.run_method("after_delete")  NOT guarded by for_reload             delete_doc.py:185
          -> Workspace.after_delete        developer_mode + module               workspace.py:163
            -> shutil.rmtree(<module>/workspace/<title>)                         export_file.py:115
      -> re-insert -> on_update -> export_to_files()   RETURNS, in_import is set  export_file.py:21

Deleted on the way in, never written back. MEASURED, not reasoned: running this against
`automations.json` on 2026-07-28 gave `before exists=True -> after exists=False`. It has really happened
on this bench three times (reimport_field_operations_desk, reimport_automations_desk_phase11,
reimport_infrastructure_desk_indexing) and went unnoticed only because the files are committed and any
git checkout put them back.

Prod is unaffected — developer_mode is off there, so `after_delete` never reaches `delete_folder`. This
is a local-dev hazard, and the cost of it is a developer losing an uncommitted desk edit.

The guard restores the bytes we read BEFORE the import, deliberately rather than re-exporting: Frappe's
own serialiser rewrites key order and whitespace, so re-exporting would "fix" the file into a diff nobody
authored.
"""
import os

import frappe
from frappe.modules.import_file import import_file_by_path


def reimport(*parts):
	"""Force one desk JSON into the DB, and leave the file exactly as it was found.

	`parts` is the app-relative path, e.g. ("workspace_sidebar", "communications.json"). Never raises: a
	desk that will not import is a cosmetic gap, and it must not fail a migrate.
	"""
	path = frappe.get_app_path("tatva_connect", *parts)
	before = _read(path)
	try:
		return bool(import_file_by_path(path, force=True))
	except Exception:
		frappe.log_error(title=f"reimport {'/'.join(parts)} failed", message=frappe.get_traceback())
		return False
	finally:
		# `finally` runs before the return value leaves, so the file is restored either way.
		_restore(path, before)


def reimport_all(paths):
	"""Force several desk JSONs, then clear the cache once. The shape every reimport patch wants."""
	for parts in paths:
		reimport(*parts)
	frappe.clear_cache()


def _read(path):
	try:
		with open(path, "rb") as f:
			return f.read()
	except OSError:
		# Nothing to protect — the caller's own import will report the missing file.
		return None


def _restore(path, before):
	"""Put the file back if the import removed it. A file that survived is left untouched."""
	if before is None or os.path.exists(path):
		return
	try:
		os.makedirs(os.path.dirname(path), exist_ok=True)
		with open(path, "wb") as f:
			f.write(before)
		frappe.logger("patches").info(f"desk reimport: restored {path}, which frappe deleted on import")
	except OSError:
		frappe.log_error(title="desk reimport: could not restore the deleted file", message=path)
