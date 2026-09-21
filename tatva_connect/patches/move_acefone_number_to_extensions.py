# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Move each rep's `acefone_number` into their Extensions table, one row per Acefone account, then retire the field."""
from collections import Counter

import frappe
from frappe.utils.fixtures import sync_fixtures

from tatva_connect.telephony import resolve
from tatva_connect.telephony.adapters import acefone

OLD_FIELD = "acefone_number"
OLD_CUSTOM_FIELD = f"{resolve.AGENT_DOCTYPE}-{OLD_FIELD}"
TITLE = "telephony: extension move"


def execute():
	if not frappe.db.exists("Custom Field", OLD_CUSTOM_FIELD):
		return
	sync_fixtures("tatva_connect")  # the Extensions table is a fixture, and fixtures sync after patches
	rows = frappe.get_all(resolve.AGENT_DOCTYPE, filters={OLD_FIELD: ["is", "set"]}, fields=["name", OLD_FIELD])
	held = Counter(row[OLD_FIELD].strip() for row in rows)
	accounts = frappe.get_all(acefone.ACCOUNT_DT, filters={"provider": acefone.PROVIDER}, pluck="name")
	for row in rows:
		seat = row[OLD_FIELD].strip()
		if held[seat] > 1:
			frappe.log_error(title=TITLE, message=f"{seat} is held by more than one rep; not moved from {row.name}")
			continue
		_add_seat(row.name, seat, accounts)
	frappe.delete_doc("Custom Field", OLD_CUSTOM_FIELD, force=True)  # authz-ok: tier-a — patch, runs at migrate


def _add_seat(agent, seat, accounts):
	doc = frappe.get_doc(resolve.AGENT_DOCTYPE, agent)
	have = {r.telephony_account for r in doc.get(resolve.SEAT_FIELD)}
	for account in accounts:
		if account not in have:
			doc.append(resolve.SEAT_FIELD, {"telephony_account": account, "extension": seat})
	doc.save(ignore_permissions=True)  # authz-ok: tier-a — patch, runs at migrate
