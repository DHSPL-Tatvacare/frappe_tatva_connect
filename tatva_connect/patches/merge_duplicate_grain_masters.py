# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Rename each grain master's older spelling to the finalised dashed one, everywhere it is stored.

The masters evolved — `GoodFlip Care` became `Goodflip-Care`, `InsideSales` became `Inside-Sales`,
`Niva Bupa` became `Niva-Bupa` — but the old spelling stayed on the master row and on everything that
named it: leads, user permissions, picklist values, task types, lead stages. Dashed is the finalised
shape. This brings every stored value onto it.

`frappe.rename_doc` does the heavy lifting: renaming the master repoints every **Link** field to it.
Three things it cannot reach, and this handles each:

  * `User Permission.for_value` is a **Dynamic** Link — `get_link_fields` collects `fieldtype='Link'`
    only — so a repped-by-grain permission would keep naming a master that no longer exists.
  * `CRM Picklist Value` stores its vertical/group/program as **Data**, not Link, so rename_doc is
    blind to them.
  * Composite **names**. A doctype named `format:{vertical}::{group}::{program}::...` has its Link
    columns repointed but its NAME left reading the old spelling — and here the name IS the identity.

Each step matches on the old spelling, not on the master still existing, so a run interrupted midway
simply completes on the next migrate. A fresh install never had the old spelling, so this no-ops.
"""
import re

import frappe

# (master doctype, old spelling, finalised dashed spelling).
_PAIRS = (
	("CRM Vertical", "Pill Up", "Pill-Up"),
	("CRM Vertical", "GoodFlip Care", "Goodflip-Care"),
	("CRM Program", "InsideSales", "Inside-Sales"),
	("CRM Program", "FieldSales", "Field-Sales"),
	("CRM Program", "Niva Bupa", "Niva-Bupa"),
	("CRM Program", "Anyra - Retina", "Anyra-Retina"),
	("CRM Program", "Eisai - Alzheimers", "Eisai-Alzheimers"),
)

_MASTERS = ("CRM Vertical", "CRM Group", "CRM Program")
_PLACEHOLDER = re.compile(r"\{(\w+)\}")

# The fieldnames this app spells a grain axis with. `psp_group` / `crm_group` are the group axis under a
# local name. Used to find the DATA columns rename_doc cannot see (CRM Picklist Value keeps all three).
_AXIS_FIELDNAMES = ("vertical", "group", "psp_group", "crm_group", "program")


def execute():
	"""SUPERSEDED, and deliberately inert — see `normalise_grain_spelling`, which runs right after.

	This patch aborted a UAT migrate on 2026-07-26. It asked "is this name taken?" with
	`WHERE BINARY name = %s`, a BYTE comparison, while the PRIMARY KEY it was protecting is
	`utf8mb4_unicode_ci` and therefore CASE-INSENSITIVE. `Tatvapractice::India::Field-Sales::Unpaid`
	sat invisibly on the key `TatvaPractice::India::Field-Sales::Unpaid` was being renamed onto, so the
	rename went out as a plain UPDATE and MariaDB raised 1062. Fifty-one rows would have done this.

	It is left as a no-op rather than repaired because its targets are also out of date: the owner
	settled the vocabulary on 2026-07-27 (`Tatvapractice`, `Goodflip`, `Pillup`, ...), and this file
	renames TOWARDS spellings that are no longer authorised — `Pill-Up` above is now `Pillup`. A patch
	that has already run somewhere cannot be corrected in place anyway; the end state is declared once,
	in the successor, which assumes nothing about whether this ever ran."""
	return


def _repoint_user_permissions(master, old, new):
	"""`for_value` is a Dynamic Link. access-04 later reconciles perms to the roster, but this keeps the
	link valid in the meantime rather than leaving it pointing at a master that was just renamed away."""
	frappe.db.sql(
		"UPDATE `tabUser Permission` SET for_value = %s WHERE allow = %s AND for_value = %s",
		(new, master, old),
	)


def _rewrite_data_axes(old, new):
	"""Fold the spelling in grain axes stored as Data. BINARY so only the exact old spelling is rewritten
	(the old values are unique across axes, so a plain value match is unambiguous)."""
	for doctype, fieldname in _data_axis_columns():
		frappe.db.sql(
			f"UPDATE `tab{doctype}` SET `{fieldname}` = %s WHERE BINARY `{fieldname}` = %s", (new, old)
		)


def _data_axis_columns():
	"""(doctype, fieldname) for every Data column that holds a grain axis."""
	rows = frappe.db.sql(
		"""SELECT parent AS dt, fieldname FROM `tabDocField` WHERE fieldtype = 'Data' AND fieldname IN %(axes)s
		   UNION ALL
		   SELECT dt, fieldname FROM `tabCustom Field` WHERE fieldtype = 'Data' AND fieldname IN %(axes)s""",
		{"axes": _AXIS_FIELDNAMES}, as_dict=True,
	)
	return [(r.dt, r.fieldname) for r in rows
	        if frappe.db.table_exists(r.dt) and frappe.db.has_column(r.dt, r.fieldname)]


def _remint_composite_names():
	"""Re-mint any composite name still carrying an old spelling, from the doctype's own `format:` autoname
	(read from the doctype, never a list kept here) applied to its now-corrected axis fields."""
	olds = [old for _m, old, _n in _PAIRS]
	for doctype, template in _composite_doctypes():
		keys = _PLACEHOLDER.findall(template)
		for name in frappe.get_all(doctype, pluck="name"):
			if not any(old in name for old in olds):
				continue
			doc = frappe.db.get_value(doctype, name, keys, as_dict=True) or {}
			want = template.format(**{k: (doc.get(k) or "") for k in keys})
			if want != name:
				# merge= when a dashed-spelled twin of this row already exists (both spellings were
				# bulk-seeded per grain), else a plain remint. Same assume-nothing reason as the master fold.
				twin = frappe.db.sql(f"SELECT 1 FROM `tab{doctype}` WHERE BINARY name = %s", (want,))
				frappe.rename_doc(doctype, name, want, merge=bool(twin), force=True)
				print(f"  merge_duplicate_grain_masters: {doctype} {name!r} -> {want!r}{' (merged)' if twin else ''}")
		frappe.db.commit()


def _composite_doctypes():
	"""(doctype, format template) for every doctype whose NAME is minted from a grain axis."""
	out = []
	for doctype, autoname in frappe.db.sql(
		"SELECT name, autoname FROM `tabDocType` WHERE autoname LIKE 'format:%%'"
	):
		if not frappe.db.table_exists(doctype):
			continue
		template = autoname[len("format:"):]
		meta = frappe.get_meta(doctype)
		for placeholder in _PLACEHOLDER.findall(template):
			df = meta.get_field(placeholder)
			if not df:
				continue
			# A grain axis, whether it is stored as a Link to a master or (CRM Picklist Value) as Data.
			if (df.fieldtype == "Link" and df.options in _MASTERS) or placeholder in _AXIS_FIELDNAMES:
				out.append((doctype, template))
				break
	return out
