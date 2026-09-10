# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Delete the Number Cards that sit on no workspace — each one a second name for a card that is placed.

WHAT THEY ARE. Eight pairs of cards carry an IDENTICAL query under two names: `WhatsApp Failed 30d` and
`Messages Failed 30d`, `WhatsApp Never Confirmed 30d` and `Sent, No Receipt 30d`, `Stuck Webhooks` and
`Stuck Inbound`, and so on. One of each pair is on a desk; the other is the name it had before a rename,
left behind because a fixture never deletes and frappe's orphan sweep covers Workspaces, Reports and
Pages but not cards. They render nowhere, so nobody has ever seen them disagree — but they are in the
Number Card list an operator browses, and a card list where the same figure appears twice under two
names is a list nobody trusts.

Five of them also shipped: `API Requests (24h)`, `API Errors (24h)`, `Partner API Requests (24h)`,
`Partner API Errors (24h)` and `Stuck Inbound` were in `fixtures/number_card.json` and are removed from
it in the same change. Removing a row from a fixture does not delete it from a site that already has it,
which is what this patch is for.

SAFE BY CONSTRUCTION: it deletes only a card that no Workspace places, so a card someone has since put
on a desk is left exactly where they put it. Idempotent, and silent about a name already gone.
"""
import frappe

# Renamed-away twins and shipped-but-unplaced cards, all verified as duplicates of a card that IS placed.
_DEAD = (
	"API Requests (24h)", "API Errors (24h)", "Partner API Requests (24h)", "Partner API Errors (24h)",
	"Stuck Inbound", "Messages Sent Today", "Messages Sent 7d", "Messages Sent 30d",
	"Messages Failed 30d", "Sent, No Receipt 30d", "Sends Not Recorded 30d",
	"Receipts Without a Message 30d", "Calls Today", "Calls 7d", "Calls 30d",
	"Calls Not Connected 30d",
)


def execute():
	placed = set(frappe.get_all("Workspace Number Card", pluck="number_card_name"))
	for name in _DEAD:
		if name in placed or not frappe.db.exists("Number Card", name):
			continue
		frappe.delete_doc("Number Card", name, force=True, ignore_permissions=True, ignore_missing=True)
