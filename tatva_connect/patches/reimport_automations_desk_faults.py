# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Automations gains a FAULT count, because a failed journey and a raised error are not the same thing.

WHAT THE PAGE COUNTED, AND WHAT IT MISSED. Every error figure on that desk was `CRM Workflow Journey.
status = 'Failed'` — a journey that gave up. Measured on this bench: eleven, ever. In the same week the
Error Log held twenty-seven errors raised on automation paths — `automation: voice call outcome unknown`,
`automation: WhatsApp send outcome unknown`, `automation disarmed: its declared parent is off`,
`voice: terminal call matches no workflow journey`. A fault of that kind leaves the journey running, so
it moves no status, writes no `failed` step outcome, and appeared in NO figure on the page. An operator
watching a card that reads one or two while the log takes thirty a day learns to distrust the card.

`Automation Faults` is that count, scoped by the prefixes this app titles its own automation-plane errors
with, grouped by reason with a day, a week and everything still retained. It is a Query Report because
the scope is an OR across prefixes and a list filter cannot express one.

`Node Outcomes` is placed at the same time. It is the step-level view — one row per step the engine
actually took — and it has existed as a chart since the fold while sitting on no workspace at all.

WHY A PATCH. `import_file.py:141` skips a standard JSON whose DB row is not older than the file, so on a
site whose desk was ever opened the bumped `modified` alone ships nothing. Goes through
`_desk.reimport_all`. No schema_setup twin: no doctype, no field, no index.
"""
from tatva_connect.patches import _desk


def execute():
	_desk.reimport_all([
		("tatva_connect", "workspace", "automations", "automations.json"),
	])
