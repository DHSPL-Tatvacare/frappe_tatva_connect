# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""The Facebook section of External Leads is re-ordered, and Facebook Pages finally has a way in.

The section read Sources, Lead Forms, Apps, Failures — the reverse of the order the job is done in, so
the first thing an operator met was the last thing they configure. It now reads Apps, Pages, Lead Forms,
Sync Sources, Failures.

Facebook Pages had NO sidebar entry at all, while the Page record holds the token the crawl actually runs
on. A crawl that stops because a page token lapsed had nowhere to be looked at.

Two navigation faults go with it, both ours:

`CRM API Request Log` was listed HERE and on Observability. With a doctype in two sidebars, frappe picks
the one whose NAME matches the doctype's module — `Observability` — so refreshing on Request Log moved the
reader out of External Leads. One doctype, one home: it stays on Observability, which owns it by module.

`Submissions` was a `URL` item, and a URL item is treated as external and opens a new tab. It was a URL
only to carry a filter, and `Workspace Sidebar Item` has a native `filters` field for exactly that. It is
now a DocType link to `CRM Lead` with the filter declared, so it stays in the tab and keeps the sidebar.

Forced rather than bumped: `import_file.py:141` skips a standard desk JSON whose DB row looks newer than
the file, so on any site whose desk has ever been opened the bumped `modified` alone migrates silently
green and changes nothing. Through `patches/_desk.reimport` and never a hand-rolled `import_file_by_path`.

Sidebar only. No schema_setup twin: no doctype, no field, no index.
"""
from tatva_connect.patches import _desk


def execute():
	_desk.reimport_all([
		("workspace_sidebar", "external_leads.json"),
	])
