# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Retire `CRM Telephony Agent Map`: the doctype, its table, and its place on the Communications desk.

IT TRANSLATES AN IDENTIFIER THE PROVIDER NO LONGER SENDS. The map exists for an agent whose provider
email is not a CRM login — the 2026-07 capture had twenty on a partner's domain and three on Gmail. The
account was reconfigured since, and `answered_agent` now carries a SEAT and no email at all: measured
across 1,195 answered calls pulled live from account 241743, exactly zero carry one. The branch that
reads this table is unreachable, and an empty table sitting beside `CRM Telephony Agent` — the one an
operator must fill for every rep — is worse than absent, because it reads as a setup step nobody did.

The seat replaced it and is strictly better: it is matched WHOLE against the value already stored to
place that rep's calls, so it needs no second configuration, and it agreed with Acefone's own
`agent_name` on all 1,195. `user_for` keeps its email step — a provider that names agents by their
corporate address still resolves with no config — and there is now no third translation table behind it.

Three end states: the doctype and its table are gone, and the Communications desk offers neither the
sidebar link nor the shortcut. The doctype folder is archived rather than deleted
(.archive/archive/doctype/), the shape `retire_task_checklists` set. Deleted through native `delete_doc`,
so a link we have NOT accounted for still fails loud; the table drop goes through `_schema.ddl`, the one
door for raw DDL. The desk goes through `_desk.reimport`: a bumped `modified` alone ships nothing on a
site whose desk was ever opened, and the sidebar travels with the workspace because a Desk tile is a Link
permitted only through a Workspace Sidebar of the same name.

Idempotent; assumes nothing about what ran before. No schema_setup twin — a fresh site no longer declares
the doctype, so nothing is ever created for this to drop.
"""

import frappe

from tatva_connect.patches import _desk, _schema

DOCTYPE = "CRM Telephony Agent Map"


def execute():
	if frappe.db.exists("DocType", DOCTYPE):
		frappe.delete_doc("DocType", DOCTYPE, force=True)  # authz-ok: tier-a — patch, runs at migrate
	# delete_doc removes the DocType row, never the table — the drop is ours, through the one door.
	if frappe.db.table_exists(DOCTYPE):
		_schema.ddl(f"DROP TABLE IF EXISTS `tab{DOCTYPE}`", f"tab{DOCTYPE}")
	_desk.reimport_all([
		("tatva_connect", "workspace", "communications", "communications.json"),
		("workspace_sidebar", "communications.json"),
	])
