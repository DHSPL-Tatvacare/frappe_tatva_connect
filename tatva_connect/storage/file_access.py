# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""WHO may read an offloaded blob. The download proxy's authorisation, and nothing else.

THE DEFECT THIS EXISTS TO CLOSE. A File row is a BOND (blob + parent), not a blob — frappe gives one
file exactly one parent, so a file that reaches a lead through a comment, an email or a ticket gets its
OWN row per surface and several rows share one blob (`file_events.on_trash` already ref-counts exactly
that). Permission is evaluated on the ROW's parent. Our proxy resolved a blob key to ONE row and judged
that row alone, so the answer depended on which row MariaDB happened to return first — a coin flip, and
when it landed on a Comment (readable by nobody) or a staged orphan (readable by its uploader) an
entitled colleague got a 403 on a file that is plainly theirs. Frappe's own gate never had this: it
loops EVERY row for the url and passes if ANY is readable (`file/utils.py:find_file_by_url`).

THE RULE. Judge the blob, not a row: pass if ANY row referencing it is readable. A surface-parented row
is read through the record the surface names — the lead, the deal, the ticket — because that is the
record the team shares and the one whose permission the business already reasons about. Nothing new is
granted: "can you read the lead" is a right every rep already holds and VAPT already blessed. Comment
and Communication keep their locked-down matrices; the IDOR fix in `access/ledger.py` stands untouched.

WHY THIS IS NOT A `has_permission` HOOK. A frappe permission hook can only ever DENY — it is ANDed with
the role matrix (`permissions.py`), so it cannot restore a read that `Comment: read=0` took away. The
proxy is our endpoint and prod's single door to every offloaded file, so the rule lives here.

GUEST IS NOT AN OWNER. Every anonymous visitor is the literal user "Guest", so an owner match would let
one visitor read another's upload — the intake form uploads Guest-owned files by design
(`intake/guards.py`). Core guards this and so does `_may_read_row`; do not drop that clause.
"""

import frappe

from tatva_connect.storage import file_manager

# surface -> (doctype-field OR literal doctype, name-field). Mirrors crm/api/activities._ATTACHMENT_SOURCES,
# which cannot be shared: it is the CRM app's private read-side map and names no helpdesk surface.
SURFACE_ROOTS = {
	"Comment": ("reference_doctype", "reference_name"),
	"Communication": ("reference_doctype", "reference_name"),
	"WhatsApp Message": ("reference_doctype", "reference_name"),
	"FCRM Note": ("reference_doctype", "reference_docname"),
	"CRM Task": ("reference_doctype", "reference_docname"),
	"HD Ticket Comment": ("HD Ticket", "reference_ticket"),
}


def root_of(doc):
	"""The record a surface-parented file really belongs to, or None when the row IS the record."""
	spec = SURFACE_ROOTS.get(doc.attached_to_doctype)
	if not spec or not doc.attached_to_name:
		return None
	dt_spec, name_field = spec
	if not frappe.db.exists("DocType", doc.attached_to_doctype):
		return None
	# A literal doctype is spelled in the map when the surface names its parent in one direction only.
	has_dt_field = frappe.get_meta(doc.attached_to_doctype).has_field(dt_spec)
	fields = [name_field] + ([dt_spec] if has_dt_field else [])
	parent = frappe.db.get_value(doc.attached_to_doctype, doc.attached_to_name, fields, as_dict=True)
	if not parent:
		return None
	root_dt = parent.get(dt_spec) if has_dt_field else dt_spec
	root_dn = parent.get(name_field)
	return (root_dt, root_dn) if root_dt and root_dn else None


def _may_read_row(doc, user):
	"""One row's verdict — core's own answer first, then the record its surface names."""
	if not doc.is_private:
		return True
	# Guest is never an owner: every anonymous visitor is the literal "Guest" (core guards this too).
	if user != "Guest" and doc.owner == user:
		return True
	from frappe.core.doctype.file.file import has_permission as core_file_has_permission

	if core_file_has_permission(doc, "read", user):
		return True
	root = root_of(doc)
	return bool(root) and bool(frappe.has_permission(root[0], "read", root[1], user=user))


def may_read_blob(blob_key, user=None, names=None):
	"""THE gate: may `user` read this blob? True if ANY row referencing it says so."""
	user = user or frappe.session.user
	for name in names if names is not None else file_manager.rows_for_blob(blob_key):
		if _may_read_row(frappe.get_doc("File", name), user):
			return True
	return False
