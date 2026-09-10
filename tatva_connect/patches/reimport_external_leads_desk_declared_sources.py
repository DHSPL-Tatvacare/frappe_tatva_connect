# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""External Leads stops counting arrivals from a typed source list and asks the ingest configuration.

THE DEFECT. Six filters on that desk named four sources as literals — `Partner API`, `FB Lead Ads`,
`Website`, `Landing Page MR Form`. Every inbound path actually stamps `CRM Lead.source` from a row an
operator configured (`api/partner.py` and `lead_sync/source.py` from a `CRM Lead API Mapping`,
`intake/intake.py` from a `CRM Intake Form`), so the literals were a snapshot of one moment's config
kept nowhere near it. Measured before this change: three of the four are declared by NO config and
carried 2,118 LeadSquared-era leads counted as arrivals, while six sources the config does declare were
counted by nothing. Over- and under-stated at once, and silent, because a literal matching nothing
raises no error.

`FB Leads per Day` was the sharpest case: `source = "FB Lead Ads"` is a string that appears nowhere in
this app's code, so the chart plotted migrated rows and would have drawn a flat zero for every real
Facebook lead. It is replaced by `Inbound by Path`, which splits the way the configuration does —
contracts against intake forms — because the Facebook sync and the partner API share a mapping and
`source` genuinely cannot tell them apart. `Web Form Leads per Day` goes for the same reason and is not
replaced: both of its sources were undeclared.

`Intake Errors per Day` matched `propagate hook failed: tatva_connect.intake.%` and so found 4 of the
160 intake errors on this site; the 156 it missed are the purpose-written `Intake form is enabled but
routes nothing`. It is now `Intake Faults per Day` over every intake error, at a week, which is inside
the fourteen days Log Settings keeps one.

WHY A PATCH. `import_file.py:141` skips a standard JSON whose DB row is not older than the file, so on
a site whose desk was ever opened the bumped `modified` alone ships nothing. Goes through
`_desk.reimport_all`, never a hand-rolled import_file_by_path. No schema_setup twin: no doctype, no
field, no index — the cards, charts and chart source are fixtures and files a fresh site imports as they
stand.
"""
from tatva_connect.patches import _desk


def execute():
	_desk.reimport_all([
		("tatva_connect", "workspace", "external_leads", "external_leads.json"),
	])
