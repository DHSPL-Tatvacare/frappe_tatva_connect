# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Communications traffic across channels: each channel's measure is its native Number Card, and the cross-channel card and charts read those cards."""
import frappe
from frappe.desk.doctype.number_card.number_card import get_result

# Per measure, the native card that counts it on each channel; their filters are the one declaration of what each measure means.
CARDS = {
	"out": {"Calls": "Calls Placed 7d", "WhatsApp": "WhatsApp Sent 7d", "Email": "Emails Sent 7d"},
	"in": {"Calls": "Calls Received 7d", "WhatsApp": "WhatsApp Received 7d", "Email": "Emails Received 7d"},
	"failed": {"Calls": "Calls Failed 7d", "WhatsApp": "WhatsApp Failed 7d", "Email": "Emails Failed 7d"},
}


def lines(measure):
	"""`{channel: Number Card doc}` for one measure."""
	if measure not in CARDS:
		frappe.throw(frappe._("Unknown communications measure {0}").format(measure))
	return {channel: frappe.get_cached_doc("Number Card", name) for channel, name in CARDS[measure].items()}


def undated(card):
	"""The card's filters without its window, so a chart can lay the same measure over its own dates."""
	return [f for f in frappe.parse_json(card.filters_json or "[]") if not (f[1] == "creation" and f[2] == "Timespan")]


@frappe.whitelist()
def card(filters=None) -> dict:
	"""The All column: the sum of each channel's own card for the measure in the card's `filters_json`, counted through frappe's card query."""
	measure = (frappe.parse_json(filters) or {}).get("measure")
	return {"value": int(sum(get_result(doc, frappe.parse_json(doc.filters_json)) for doc in lines(measure).values())), "fieldtype": "Int"}
