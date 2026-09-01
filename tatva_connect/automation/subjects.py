"""The ONE brain for automation SUBJECTS — which doctypes the engine anchors a rule to, and how each
resolves to its grain-bearing CRM Lead.

A subject is a doctype a Field-Changed rule may watch and whose grain the engine can resolve. Adding a
subject = one entry here + its `on_update` hook in hooks.py (the migrate drift gate,
`automation.drift`, fails the build if the hook is missing). Nothing else — no config table: you cannot
watch a doctype without a code-level hook + deploy anyway, so participation is intrinsically code.

This map is the single source of truth for: the catalog doctype gate (a can_watch row must name a
subject), the drift gate's doctype list, and `watch._subject`'s Lead resolution. No parallel list.
"""

# link=None → the doc IS the lead. Otherwise `link` is the fieldname pointing at the CRM Lead; a
# dynamic link (CRM Task's reference_doctype/reference_docname) adds a guard_field/guard_value pair so
# we only resolve when it actually points at a CRM Lead.
SUBJECTS = {
	"CRM Lead": {"link": None},
	"CRM Task": {"link": "reference_docname", "guard_field": "reference_doctype", "guard_value": "CRM Lead"},
	# A Deal is the customer a Lead became and names it in `lead`; a Link to CRM Lead needs no guard.
	"CRM Deal": {"link": "lead"},
	# A File resolves to the lead it is attached to (attached_to_doctype/attached_to_name) — the guard
	# keeps a File attached to anything else (a Task, a Note) from ever resolving to a lead.
	"File": {"link": "attached_to_name", "guard_field": "attached_to_doctype", "guard_value": "CRM Lead"},
	# A WhatsApp Message resolves to its linked lead via reference_name (NOT reference_docname — the
	# WhatsApp Message field is reference_name); the guard pins it to a CRM Lead reference only.
	"WhatsApp Message": {"link": "reference_name", "guard_field": "reference_doctype", "guard_value": "CRM Lead"},
	# A call carries the same dynamic pair a Task does, so it resolves the same way and needs the same guard.
	"CRM Call Log": {"link": "reference_docname", "guard_field": "reference_doctype", "guard_value": "CRM Lead"},
}


# What a workflow may WRITE beyond the lead and the doc that fired it — SUBJECTS' twin, and code for the
# same reason: participation needs a deploy anyway, and a one-line PR is visible in review where a row is not.
#
# `lead_field` is where the engine stamps the patient, and the key it reads back to find a record this
# workflow already raised for them — a journey is per-save, so without it a patient who writes five times
# collects five tickets. It holds the lead's NAME as Data and is deliberately not a Link: a Link from a
# write target back to CRM Lead would make frappe refuse to delete a lead that has one (breaking bulk
# delete), and would bind that target's visibility to any User Permission ever created on CRM Lead. The
# engine stores its own back-references this way already (CRM Task.custom_workflow_token).
# `status_doctype` names the master whose `category` says whether a record is still
# open; the categories are READ off it, never a list of status names typed here, because an operator adds
# their own statuses and a typed copy would silently stop recognising them. A target may declare neither,
# and is then simply raised anew each time.
WRITE_TARGETS = {
	"HD Ticket": {"lead_field": "custom_lead", "status_doctype": "HD Ticket Status"},
}

# Which statuses mean FINISHED — named as the terminal set, not the open one, and that direction is the
# whole point. Helpdesk's category vocabulary is Open / Paused / Resolved today; listing the two open ones
# would mean a category added tomorrow falls outside the list, every record in it reads as finished, and
# the patient collects a second record — the exact defect this reuse exists to prevent. Named this way an
# unknown category reads as still open, so the failure is a reuse nobody asked for rather than a duplicate.
# (Paused is not terminal: a record waiting on somebody is still owed an answer.)
TERMINAL_CATEGORIES = ("Resolved",)


def is_subject(doctype):
	"""True if the engine can anchor a Field-Changed rule to this doctype (i.e. resolve its grain)."""
	return doctype in SUBJECTS


def is_write_target(doctype):
	"""True if a workflow may create and write this doctype — `is_subject`'s twin, asked of the same brain."""
	return doctype in WRITE_TARGETS


def subject_doctypes():
	"""The doctypes that participate in the Field-Changed trigger — the drift gate's authoritative list."""
	return list(SUBJECTS)


def resolve_lead_name(doc):
	"""The CRM Lead name a subject doc resolves to, or None (fail-closed — no lead ⇒ no rule fires).
	Lead → itself; Task → its parent lead (only when reference_doctype is CRM Lead). One resolver, no
	per-doctype if/else scattered across the engine."""
	spec = SUBJECTS.get(doc.doctype)
	if spec is None:
		return None
	if spec["link"] is None:
		return doc.name
	if spec.get("guard_field") and doc.get(spec["guard_field"]) != spec.get("guard_value"):
		return None
	return doc.get(spec["link"]) or None
