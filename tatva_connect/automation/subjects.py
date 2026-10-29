"""The ONE brain for automation SUBJECTS — which doctypes the engine anchors a rule to, how each
resolves to its CRM Lead, and, for one that declares `grain_from`, the grain it names for itself when no lead stands behind it.

A subject is a doctype a Field-Changed rule may watch and whose grain the engine can resolve. Adding a
subject = one entry here. Nothing else — no config table and no per-doctype hook: every doctype is
already covered by the wildcard front-door (`hooks.doc_events["*"]`, asserted by
`drift._assert_engine_wired`), and a doctype only acts because a workflow names it. Participation is
intrinsically code because this map is code.

This map is the single source of truth for: the catalog doctype gate (a can_watch row must name a
subject), the drift gate's doctype list, and `watch._subject`'s Lead resolution. No parallel list.
"""

import frappe

LEAD_DT = "CRM Lead"

# link=None → the doc IS the lead. Otherwise `link` is the fieldname pointing at the CRM Lead; a
# dynamic link (CRM Task's reference_doctype/reference_docname) adds a guard_field/guard_value pair so
# we only resolve when it actually points at a CRM Lead; `through` is a declared second hop for a doc
# whose own parent is a surface (a file on an email) rather than the patient.
# `grain_from` reads the grain a doc names for itself, for a Trigger that also runs without a lead.
SUBJECTS = {
	"CRM Lead": {"link": None},
	"CRM Task": {"link": "reference_docname", "guard_field": "reference_doctype", "guard_value": "CRM Lead"},
	# A Deal is the customer a Lead became and names it in `lead`; a Link to CRM Lead needs no guard.
	"CRM Deal": {"link": "lead"},
	# A File attached straight to the lead resolves by the guard; one attached to a SURFACE the lead owns —
	# an email, a comment, a task, a WhatsApp message — resolves through `file_access.root_of`, which is
	# already the ONE brain for "the record a surface-parented file really belongs to". An emailed document
	# is the patient's whether it arrived on the lead or on the reply, and `through` is what says so.
	"File": {"link": "attached_to_name", "guard_field": "attached_to_doctype", "guard_value": "CRM Lead",
	         "through": "tatva_connect.storage.file_access.root_of"},
	# A WhatsApp Message resolves to its linked lead via reference_name (NOT reference_docname — the
	# WhatsApp Message field is reference_name); the guard pins it to a CRM Lead reference only.
	"WhatsApp Message": {"link": "reference_name", "guard_field": "reference_doctype", "guard_value": "CRM Lead"},
	# A call carries the same dynamic pair a Task does, so it resolves the same way and needs the same guard.
	"CRM Call Log": {"link": "reference_docname", "guard_field": "reference_doctype", "guard_value": "CRM Lead",
	                 "grain_from": "tatva_connect.telephony.resolve.grain_of_call"},
	# An email. A reply to one a rep sent from the CRM inherits the parent's reference (frappe's receiver,
	# `email/receive.py`), which is what puts it on a patient; a cold email from a stranger references
	# nothing and resolves to no lead, so no workflow can act on it. Same dynamic pair, same guard.
	"Communication": {"link": "reference_name", "guard_field": "reference_doctype", "guard_value": "CRM Lead"},
	# A ticket names its lead in Data, not a Link (see WRITE_TARGETS below) — so the name can outlive the lead, and `context.subject` reads an unresolvable one as no subject.
	# With no lead, a ticket runs on the grain it was stamped with at birth.
	"HD Ticket": {"link": "custom_lead", "grain_from": "tatva_connect.taxonomy.grain.own_of"},
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
# `find` names the one function that answers "which record" for the lead instead, finding or making it.
WRITE_TARGETS = {
	"HD Ticket": {"lead_field": "custom_lead", "status_doctype": "HD Ticket Status"},
	# The person a lead becomes, found by phone first, then email, through the one contact brain.
	"Contact": {"find": "tatva_connect.helpdesk.contact.contact_for_lead"},
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


def runs_without_lead(doctype):
	"""True if this subject names its own grain, so a Trigger on it may also run when no lead stands behind it."""
	return bool((SUBJECTS.get(doctype) or {}).get("grain_from"))


def own_axes(doc):
	"""(vertical, group, program) the doc names for itself through its declared `grain_from`, or None (fail-closed), all-blank included."""
	reader = (SUBJECTS.get(doc.doctype) or {}).get("grain_from")
	axes = frappe.get_attr(reader)(doc) if reader else None
	return axes if axes and any(axes) else None


def resolve_lead_name(doc):
	"""The CRM Lead name a subject doc resolves to, or None; with no lead, a subject declaring `grain_from` still runs on its own grain.
	Lead → itself; Task → its parent lead (only when reference_doctype is CRM Lead). One resolver, no
	per-doctype if/else scattered across the engine — a subject that reaches its lead some other way
	declares `through` rather than earning a branch here."""
	spec = SUBJECTS.get(doc.doctype)
	if spec is None:
		return None
	if spec["link"] is None:
		return doc.name
	if spec.get("guard_field") and doc.get(spec["guard_field"]) != spec.get("guard_value"):
		return _through(spec, doc)
	return doc.get(spec["link"]) or None


def resolve_lead_name_of(doctype, name):
	"""`resolve_lead_name` for a record not yet loaded: a lead answers with its own name, without being read."""
	spec = SUBJECTS.get(doctype)
	if spec is None:
		return None
	if spec["link"] is None:
		return name
	return resolve_lead_name(frappe.get_doc(doctype, name))


def _through(spec, doc):
	"""The declared second hop, for a subject whose own parent is a surface rather than the patient.

	Fail-closed twice over: no `through` and there is no second hop, and a root that is not a CRM Lead —
	a file on a helpdesk ticket, a comment on a deal — resolves to nothing exactly as before.
	"""
	if not spec.get("through"):
		return None
	root = frappe.get_attr(spec["through"])(doc)
	return root[1] if root and root[0] == LEAD_DT else None
