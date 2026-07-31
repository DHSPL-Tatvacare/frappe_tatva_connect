# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The hover-preview payload for ONE lead — a contact card, not a form.

SIX THINGS, and the list is closed: who they are (name, phone, photo), where they are (stage), who owns
them, where they came from, and which grain they belong to. Decided 2026-07-31 after the first cut read
the whole side-panel layout: a card that grows with an operator's layout edit is a card nobody designed.

The SERVER decides what those six are, here, once. A caller that could name fields would be a second
declaration of what summarises a lead (R2) AND a way to ask for a field the card was never meant to show
(BOPLA), so the caller sends a lead id and nothing else.

THE STAGE CARRIES ITS OWN COLOUR, because the colour is the stage master's data — not a client map. The
same badge renders on the spotlight search, which is why the colour rides in the payload instead of being
looked up twice; `TatvaStageBadge.vue` is the one renderer.

Per-document counterpart of `list_link_titles.get_doc_link_titles`: same shape, same gate, ONE call per
card open, never one per field. The card renders what it is handed and derives nothing (E2).

A lead that is not there and a lead the caller may not read answer with the same `PermissionError`: a
control that fires on mouse-over must never be a way to ask which record ids exist.
"""

import frappe

from tatva_connect.taxonomy import labels

LEAD = "CRM Lead"

# The grain axes, in reading order. Fieldnames are the taxonomy's own; the labels are the rep's words.
_GRAIN = (("custom_vertical", "Product Line"), ("custom_group", "Group"), ("custom_current_program", "Program"))


@frappe.whitelist()
def get_lead_preview(name):
	"""The card's payload for one lead. One document read; nothing derived on the client."""
	try:
		frappe.has_permission(LEAD, "read", doc=name, throw=True)
		doc = frappe.get_cached_doc(LEAD, name)
	except frappe.DoesNotExistError:
		# The one answer for missing and for unreadable, so a refusal never confirms that an id exists.
		raise frappe.PermissionError

	# The ONE stage reading, shared with the spotlight index so the two surfaces cannot disagree.
	stage_label, stage_color = labels.stage_of(doc)
	return {
		"name": doc.name,
		"title": doc.lead_name or doc.first_name or doc.name,
		"image": doc.image or "",
		"phone": doc.mobile_no or "",
		"stage": stage_label,
		"stage_color": stage_color,
		# The owner's email only. The browser already resolves a user to a name and an avatar through its
		# own users store — the same one the Assigned To column reads — so resolving it here would be a
		# second answer to "who is this person", and a slower one.
		"owner": doc.lead_owner or "",
		"source": doc.source or "",
		"grain": [{"label": label, "value": doc.get(f)} for f, label in _GRAIN if doc.get(f)],
	}
