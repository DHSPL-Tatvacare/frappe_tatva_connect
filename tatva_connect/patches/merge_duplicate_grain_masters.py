# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Fold each grain master's older spelling into the dashed one, and re-mint every key that carried it.

The masters evolved — `Niva Bupa` became `Niva-Bupa`, `InsideSales` became `Inside-Sales` — and each
change landed as a NEW row beside the old one instead of replacing it. So the CRM ended up holding two
of everything for those grains: two lead stage sets, two picklist sets, two task type sets, and leads
and user permissions split across both. Dashed is the finalised shape; these are the same values, so
they become one.

Two halves, and the second is the one a plain rename misses:

  * The MASTER rows fold with `rename_doc(merge=True)`, which repoints every Link field for us — EXCEPT
    `User Permission.for_value`, which is a Dynamic Link. `rename_doc` collects `fieldtype = "Link"`
    only (`model/rename_doc.py:get_link_fields`), so those rows would keep naming a master that no
    longer exists and every rep scoped by one would silently lose their access. They are repointed here.

  * The COMPOSITE KEYS. Nine doctypes mint their primary key from the grain
    (`format:{vertical}::{group}::{program}::...`). Renaming the master fixes their Link COLUMN and
    leaves their NAME reading the old spelling — and in this app the composite key IS the identity.
    Each such row is re-minted from its doctype's own `autoname`, so nothing here restates a naming
    rule that the doctype already declares.

Idempotent: a spelling already folded has no row left to fold, and a key already re-minted already
equals what its format renders. A fresh install has neither duplicate, so this no-ops there.
"""
import re

import frappe

# (master doctype, the spelling that goes, the spelling that stays). Dashed stays — operator decision.
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

# The axis names this app spells a grain with. `psp_group` (routing/telephony) and `crm_group` (the API
# mapping) are the same axis under a local name. Needed because not every one of these is a Link: CRM
# Picklist Value keeps all three as DATA, so `rename_doc` — which updates Link fields only — cannot see
# them, and 110 rows kept a folded spelling in both their column and their key.
_AXIS_FIELDNAMES = ("vertical", "group", "psp_group", "crm_group", "program")


def execute():
	for master, loser, winner in _PAIRS:
		_fold_master(master, loser, winner)
		_rewrite_text_axes(loser, winner)
	_remint_composite_keys()
	frappe.db.commit()


def _fold_master(master, loser, winner):
	"""One spelling folds into the other. `exists` is the DB's own comparison, so it answers about the
	row that is really there rather than about the characters we happen to have typed."""
	if not frappe.db.exists(master, loser):
		return
	if not frappe.db.exists(master, winner):
		# The dashed row was never seeded on this site: the old row simply takes the new name.
		frappe.rename_doc(master, loser, winner, force=True)
	else:
		_repoint_user_permissions(master, loser, winner)
		frappe.rename_doc(master, loser, winner, merge=True, force=True)
	print(f"  merge_duplicate_grain_masters: {master} {loser!r} -> {winner!r}")
	frappe.db.commit()


def _repoint_user_permissions(master, loser, winner):
	"""`for_value` is a Dynamic Link, so `rename_doc` never touches it. A row left naming the folded
	master scopes its user to nothing. Rows that would collide with an existing one are dropped rather
	than repointed — the surviving row already grants exactly the same thing."""
	for perm in frappe.get_all(
		"User Permission", filters={"allow": master, "for_value": loser}, fields=["name", "user", "applicable_for", "apply_to_all_doctypes"]
	):
		twin = frappe.db.exists("User Permission", {
			"user": perm.user, "allow": master, "for_value": winner,
			"applicable_for": perm.applicable_for or "", "apply_to_all_doctypes": perm.apply_to_all_doctypes,
		})
		if twin:
			frappe.delete_doc("User Permission", perm.name, ignore_permissions=True, force=True)  # authz-ok: tier-a — patch, the surviving twin grants the same scope
		else:
			frappe.db.set_value("User Permission", perm.name, "for_value", winner, update_modified=False)


def _rewrite_text_axes(loser, winner):
	"""Fold the spelling in grain axes stored as TEXT. A Link column is repointed by `rename_doc`; a Data
	column holding the same value is invisible to it. BINARY so only the exact old spelling is rewritten."""
	for doctype, fieldname in _text_axis_columns():
		frappe.db.sql(
			f"UPDATE `tab{doctype}` SET `{fieldname}` = %s WHERE BINARY `{fieldname}` = %s", (winner, loser)
		)


def _text_axis_columns():
	"""(doctype, fieldname) for every Data column that spells a grain axis."""
	rows = frappe.db.sql(
		"""SELECT parent AS dt, fieldname FROM `tabDocField` WHERE fieldtype = 'Data' AND fieldname IN %(axes)s
		   UNION ALL
		   SELECT dt, fieldname FROM `tabCustom Field` WHERE fieldtype = 'Data' AND fieldname IN %(axes)s""",
		{"axes": _AXIS_FIELDNAMES},
		as_dict=True,
	)
	return [(r.dt, r.fieldname) for r in rows
	        if frappe.db.table_exists(r.dt) and frappe.db.has_column(r.dt, r.fieldname)]


def _remint_composite_keys():
	"""Re-mint any key whose rendered format no longer matches its own name.

	Driven by the doctype's `autoname`, never by a list kept here: the doctype declares how it is named
	and this reads that declaration. A row whose target name already exists is MERGED into it — after
	the fold both spellings describe one thing, which is the whole point.
	"""
	for doctype, template in _composite_doctypes():
		for name in frappe.get_all(doctype, pluck="name"):
			doc = frappe.db.get_value(doctype, name, _PLACEHOLDER.findall(template), as_dict=True) or {}
			want = template.format(**{k: (doc.get(k) or "") for k in _PLACEHOLDER.findall(template)})
			if want == name or not want.strip(":"):
				continue
			# BINARY: a name differing from `want` only by case is this same row, not a twin to merge into.
			twin = frappe.db.sql(f"SELECT 1 FROM `tab{doctype}` WHERE BINARY name = %s", (want,))
			frappe.rename_doc(doctype, name, want, merge=bool(twin), force=True)
			print(f"  merge_duplicate_grain_masters: {doctype} {name!r} -> {want!r}{' (merged)' if twin else ''}")
		frappe.db.commit()


def _composite_doctypes():
	"""(doctype, format template) for every doctype whose NAME is minted from a grain master Link."""
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
			# A grain axis, however it is stored: a Link to one of the masters, or the same value as Data.
			if (df.fieldtype == "Link" and df.options in _MASTERS) or placeholder in _AXIS_FIELDNAMES:
				out.append((doctype, template))
				break
	return out
