"""The ONE brain for automation SUBJECTS — which doctypes the engine anchors a rule to, and how each
resolves to its grain-bearing CRM Lead.

A subject is a doctype a Field-Changed rule may watch and whose grain the engine can resolve. Adding a
subject = one entry here + its `on_update` hook in hooks.py (the migrate drift gate,
`automation.drift`, fails the build if the hook is missing). Nothing else — no config table: you cannot
watch a doctype without a code-level hook + deploy anyway, so participation is intrinsically code.

This map is the single source of truth for: the allowlist doctype gate (a can_read / can_watch row must
name a subject), the drift gate's doctype list, and `watch._subject`'s Lead resolution. No parallel list.
"""

# link=None → the doc IS the lead. Otherwise `link` is the fieldname pointing at the CRM Lead; a
# dynamic link (CRM Task's reference_doctype/reference_docname) adds a guard_field/guard_value pair so
# we only resolve when it actually points at a CRM Lead.
SUBJECTS = {
	"CRM Lead": {"link": None},
	"CRM Task": {"link": "reference_docname", "guard_field": "reference_doctype", "guard_value": "CRM Lead"},
	# A File resolves to the lead it is attached to (attached_to_doctype/attached_to_name) — the guard
	# keeps a File attached to anything else (a Task, a Note) from ever resolving to a lead.
	"File": {"link": "attached_to_name", "guard_field": "attached_to_doctype", "guard_value": "CRM Lead"},
	# A WhatsApp Message resolves to its linked lead via reference_name (NOT reference_docname — the
	# WhatsApp Message field is reference_name); the guard pins it to a CRM Lead reference only.
	"WhatsApp Message": {"link": "reference_name", "guard_field": "reference_doctype", "guard_value": "CRM Lead"},
}


def is_subject(doctype):
	"""True if the engine can anchor a Field-Changed rule to this doctype (i.e. resolve its grain)."""
	return doctype in SUBJECTS


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
