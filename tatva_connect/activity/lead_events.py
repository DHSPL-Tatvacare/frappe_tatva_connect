"""How a record's own history becomes a rail line — ONE declaration, three readers.

A field edit IS a record: the `Version` row frappe writes for the save. The rail indexes that row like any
other source, so one save is one event and a reader can page back past frappe's ten-version window. What a
save SHOWS is `rail_changes` below — asked by the index writer to decide whether to index, and by the rail
to decide what to draw, so a pointer exists exactly when a row renders.

WHY IT LIVES HERE. The rule was written inline and byte-identical TWICE in the crm fork
(`get_lead_activities` and `get_deal_activities`). The rail needs the same lines without paying for
`get_docinfo` — 0.46 s and 178 queries on the fattest dev lead, because that call also loads assignments,
likes, energy points and attachments the rail never reads. Copying the rule a third time would grow a
second brain, so it moves here and both fork readers delegate to it in one line.

PARITY IS THE POINT. `VERSION_WINDOW` and the `track_changes` guard are frappe's own
(`desk/form/load.py:get_versions`), reproduced here so the fast path shows exactly the history the
dormant path shows — no more, no less. The one deliberate divergence is noted on `field_changes`.
"""
import json

import frappe
from frappe import _

# Frappe shows a record's ten most recent edits and no further back (get_versions). The rail inherits
# that window rather than choosing its own, or the two paths would disagree about how far history goes.
VERSION_WINDOW = 10

# Fields whose edits are noise on a timeline — SLA bookkeeping, and the link each doctype is defined by.
_AVOID_FIELDS = {
	"CRM Lead": ("converted", "response_by", "sla_creation", "sla", "first_response_time", "first_responded_on"),
	"CRM Deal": ("lead", "response_by", "sla_creation", "sla", "first_response_time", "first_responded_on"),
}

_CREATION_TEXT = {"CRM Lead": "created this lead", "CRM Deal": "created this deal"}

# Derived columns a reader must never be shown: custom_stage follows custom_substage — the same move written twice.
NOISE_FIELDS = {"custom_stage"}


def rail_changes(doctype: str, version) -> list:
	"""The changes ONE save actually shows, or []. The index writer asks it whether to index a Version and
	the rail asks it what to draw, so a pointer exists exactly when a row renders and a page is never short."""
	return [
		c for c in field_changes(doctype, [version], doctype == "CRM Lead")
		if (c.get("data") or {}).get("field") not in NOISE_FIELDS
	]


def recent_versions(doctype: str, name: str) -> list:
	"""The edits a reader may see — frappe's own window, asked of the Version table directly.

	`get_docinfo` answers the same question, but it answers nine others at the same time. One query here."""
	if not frappe.get_meta(doctype).track_changes:
		return []
	return frappe.get_all(
		"Version",
		filters={"ref_doctype": doctype, "docname": str(name)},
		fields=["name", "owner", "creation", "data"],
		limit=VERSION_WINDOW,
		order_by="creation desc",
	)


def field_changes(doctype: str, versions: list, is_lead: bool) -> list:
	"""Version rows to the timeline's changed / added / removed lines, in the order given.

	A version carrying no readable change contributes nothing — the fork's inline loop appended the
	previous iteration's line again in that case, which was a latent duplicate, never a rendered feature.
	"""
	# Imported inside the call: crm's module imports this one, so a module-level import is a cycle.
	from crm.api.activities import ATTACHMENT_FIELDTYPES, attachment_label, is_translatable

	fields = {
		f.fieldname: {"label": f.label, "options": f.options, "fieldtype": f.fieldtype}
		for f in frappe.get_meta(doctype).fields
	}
	avoid = _AVOID_FIELDS.get(doctype, ())
	out = []
	# EVERY change a save recorded, not just the first: one save touching five fields is five lines, as crm's own loop produced.
	changes = [(v, c) for v in versions for c in json.loads(v.data).get("changed") or []]
	for version, change in changes:
		field = fields.get(change[0])
		if not field or change[0] in avoid or (not change[1] and not change[2]):
			continue

		field_label = field.get("label") or change[0]
		field_option = field.get("options") or None

		if not change[1] and change[2]:
			activity_type = "added"
			data = {"field": change[0], "field_label": field_label, "value": change[2]}
		elif change[1] and not change[2]:
			activity_type = "removed"
			data = {"field": change[0], "field_label": field_label, "value": change[1]}
		else:
			activity_type = "changed"
			data = {"field": change[0], "field_label": field_label, "old_value": change[1], "value": change[2]}

		# An Attach value is a file_url — show the file's name, never our storage URL (M3).
		if field.get("fieldtype") in ATTACHMENT_FIELDTYPES:
			if data.get("value"):
				data["value"] = attachment_label(data["value"])
			if data.get("old_value"):
				data["old_value"] = attachment_label(data["old_value"])

		if data.get("value") and field_option and is_translatable(field_option):
			data["value"] = _(data["value"])
			if data.get("old_value"):
				data["old_value"] = _(data["old_value"])

		out.append({
			"activity_type": activity_type,
			"creation": version.creation,
			"owner": version.owner,
			"data": data,
			"is_lead": is_lead,
			"options": field_option,
		})
	return out


def creation_event(doctype: str, name: str) -> dict:
	"""The first line every timeline ends on — the record being created."""
	created, owner = frappe.db.get_value(doctype, name, ["creation", "owner"]) or (None, None)
	return {
		"activity_type": "creation",
		"creation": created,
		"owner": owner,
		"data": _(_CREATION_TEXT.get(doctype, "created this record")),
		"is_lead": doctype == "CRM Lead",
	}


# `history()` removed here when a save became a rail event — no callers left; archived in .archive/.
