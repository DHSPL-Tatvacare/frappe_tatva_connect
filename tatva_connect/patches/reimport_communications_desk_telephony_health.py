# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Communications gains the two telephony cuts nothing in this app could answer, and the report behind them.

WHAT WAS MISSING, AND WHY NO EXISTING FIGURE COULD SHOW IT.

`reference_docname` appeared in no card, no chart and no report anywhere. Attribution matches a caller's
number against the receiving account's grouping and drops the call when nothing matches (A.16), so a call
can land, be answered, and reach no lead — and every figure we had described how the call ENDED, never
whether it arrived anywhere. `Calls With No Lead 7d` is that count.

A call that produced no media row at all was invisible for a structural reason: `Asset Inventory` reads
the ledger from the MEDIA side (`FROM tabCRM Call Media JOIN tabCRM Call Log`), so a call with no row
contributes to nothing. No state count can ever show it, because the case IS the absence of a row. The
new report reads from the CALL side with a LEFT JOIN, which is the only shape that can.

`Telephony Health` carries both beside the recording split, the attempt ceiling and how long each medium
has been quiet — one row per medium and direction, over seven days, no filter but the window.

WHY A PATCH. `import_file.py:141` skips a standard JSON whose DB row is not older than the file, so on a
site whose desk was ever opened the bumped `modified` alone ships nothing. Goes through
`_desk.reimport_all`, never a hand-rolled import_file_by_path. No schema_setup twin: no doctype, no
field, no index.
"""
from tatva_connect.patches import _desk


def execute():
	_desk.reimport_all([
		("tatva_connect", "workspace", "communications", "communications.json"),
	])
