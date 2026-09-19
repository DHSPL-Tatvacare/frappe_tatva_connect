"""The Connections panel on Facebook Lead Form: which source, if any, crawls this form.

A form with no source is discovered and never read, and that state looks exactly like a form that works —
nothing errors, leads simply never arrive. The panel answers it in one number: `Lead Sync Source 0` is
the whole diagnosis, on the screen an operator is already standing on after mapping the form.

A CUSTOM DocType Link rather than a fork edit. `Facebook Lead Form` is the CRM fork's doctype and its own
`links` is empty; `meta.add_custom_links_and_actions` merges rows carrying `custom=1` at read time, so
this survives every re-sync of the standard JSON without the file being touched. The same order the
constitution sets: Property Setter before fork, and this is that seam for a child table a Property Setter
cannot reach.

`parentfield` is deliberately BLANK. The two readers key differently: the DocType's own child-table load
finds rows by `parentfield`, while `add_custom_links_and_actions` finds them by `parent` + `custom`. A row
carrying both is found twice and the panel renders twice — measured, the same row id listed two times.
Leaving `parentfield` empty leaves exactly one reader, which is the one that belongs to customisation.
"""
import frappe

# (parent doctype, the doctype to show, the Link field on it that points back here)
_LINKS = [
	("Facebook Lead Form", "Lead Sync Source", "facebook_lead_form"),
]


def ensure_rows():
	"""Idempotent: the row is asserted on every migrate, and an operator's ordering is left alone."""
	for parent, link_doctype, link_fieldname in _LINKS:
		if not frappe.db.exists("DocType", parent):
			continue  # a site without the fork's doctype has nothing to hang the panel on
		existing = frappe.db.get_value(
			"DocType Link",
			{"parent": parent, "link_doctype": link_doctype, "link_fieldname": link_fieldname},
			["name", "parentfield"],
			as_dict=True,
		)
		if existing:
			# Repairs a row written before the parentfield rule was understood; otherwise a no-op.
			if existing.parentfield:
				frappe.db.set_value("DocType Link", existing.name, "parentfield", "", update_modified=False)
				frappe.clear_cache(doctype=parent)
			continue
		frappe.get_doc({
			"doctype": "DocType Link",
			"parent": parent,
			"parenttype": "DocType",
			"parentfield": "",
			"link_doctype": link_doctype,
			"link_fieldname": link_fieldname,
			"custom": 1,
		}).insert(ignore_permissions=True)  # authz-ok: tier-c — after_migrate, no session user
		frappe.clear_cache(doctype=parent)
