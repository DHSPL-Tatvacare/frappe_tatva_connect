# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Automations finally shows Health by Grain — the chart `hooks.py` has claimed exists since Workspace-P2.

The chart shipped as a fixture naming the source "Automation Health by Grain", and no folder in this app
ever provided one, so it resolved to nothing and sat on no workspace either: dead twice over, while a
comment in `hooks.py` stated it was live. The source now exists and the chart is placed.

It splits by the grain of the LEAD each journey ran on, read from `grain.columns("CRM Lead")` off the
schema — not the workflow's `trigger_vertical/group/program`, which are Data fields holding whatever an
author typed to scope a trigger and which `grain.columns` correctly refuses to treat as grain columns.

A bumped `modified` alone ships nothing on a desk that was ever opened (import_file.py:141), and
`reimport_automations_desk_faults` has already run, so this needs its own line.
"""
from tatva_connect.patches import _desk


def execute():
	_desk.reimport_all([
		("tatva_connect", "workspace", "automations", "automations.json"),
	])
