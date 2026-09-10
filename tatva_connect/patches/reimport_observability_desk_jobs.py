# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Observability gains a third page, Jobs, and the sidebar entry that reaches it.

Declared end state: the Observability sidebar carries `Jobs` under `Assets`, opening a workspace that
states the health of the background tier in figures — a halted scheduler, a lane holding work no worker
serves, failed and queued jobs, today's errors — over four charts and the two reports behind them.

WHY IT EXISTS. Every timed and deferred thing on this site runs on a worker, and a worker failure is
silent: no screen turns red, the work simply never happens. `RQ Job` and `RQ Worker` are virtual
doctypes over redis and can carry no card, chart or report, so the figures come from `Jobs Health`, a
Dashboard Chart Source reading the same brain the cards call, and the live rows stay a link away.

WHAT IT DELIBERATELY DOES NOT SHOW. Counts only — no job name, argument, traceback or site config
reaches the page. A traceback is read on the `RQ Job` row or in `Error Log`, each under its own
permission, and the platform's configuration in `CRM Control Tower`, which this page finally links:
until now it was a virtual Single reachable from no workspace, no sidebar and no shortcut.

WHY A PATCH AT ALL. Only the sidebar needs one. `import_file.py:141` skips a standard JSON whose DB row
is not older than the file, so on any site whose desk was ever opened the added item migrates silently
green; the bumped `modified` is necessary and not sufficient. A separate line from
`reimport_observability_desk_assets` because an applied patch never runs again. Goes through
`_desk.reimport_all`, never a hand-rolled import_file_by_path.

No schema_setup twin: no doctype, no field, no index. A fresh site imports every JSON as it stands.
"""
from tatva_connect.patches import _desk


def execute():
	_desk.reimport_all([
		("workspace_sidebar", "observability.json"),
	])
