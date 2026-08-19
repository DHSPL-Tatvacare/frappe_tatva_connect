__version__ = "0.0.1"

# The lead/deal read predicate is replaced HERE and nowhere else: frappe ANDs every app's
# `permission_query_conditions` (db_query.py:1160), so ours cannot replace crm's by being registered
# beside it — the swap has to happen on the function itself. This module is imported before any hook is
# resolved, in every process (web, worker, scheduler), which is the property the swap needs.
#
# A failure is SAFE — crm's own rule stays in place, correct and merely slow — but it must not be
# SILENT, or the switch would read as armed while every query still took the slow path. The warning is
# skipped when frappe itself is absent, because that is a static-analysis run and not a live app.
try:
	from tatva_connect.access.record_access import install as _install_record_access

	_install_record_access()
except Exception as exc:  # pragma: no cover - crm absent during install, or any import-time fault
	try:
		import frappe  # noqa: F401
	except ImportError:
		pass  # no site, no app: a lint or an AST test, nothing to warn about
	else:
		print(f"tatva_connect: record access override NOT installed ({exc}); crm's own rule is in effect")
