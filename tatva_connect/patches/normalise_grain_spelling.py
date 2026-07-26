# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Fold every grain axis onto the ONE authorised spelling, everywhere it is stored.

The grain vocabulary was spelled several ways over its life — `TatvaPractice` and `Tatvapractice`,
`GoodFlip` and `Goodflip`, `GoodFlip Care`, `FieldSales`, `InsideSales`, `Pill Up`, `Pill-Up`. The
owner settled it on 2026-07-27: the tuple below is the whole vocabulary and the only spelling of it.
This patch states that end state and brings the database to it, whatever it was carrying before.

WHY THE PREVIOUS ATTEMPT ABORTED A MIGRATE, and what is different here.

`merge_duplicate_grain_masters` asked "is this name taken?" with `WHERE BINARY name = %s`. That is a
BYTE comparison; the PRIMARY KEY it is protecting is `utf8mb4_unicode_ci`, which is CASE-INSENSITIVE.
So `Tatvapractice::India::Field-Sales::Unpaid` sat invisibly on the key that
`TatvaPractice::India::Field-Sales::Unpaid` was about to be renamed onto, the rename was issued as a
plain UPDATE, and MariaDB raised 1062 — 51 rows deep in a UAT deploy.

`frappe.rename_doc` cannot be trusted to catch it either: it finds the row (its check runs under the
collation) and then DISCARDS the match when the bytes differ (`rename_doc.py:360`, "for fixing case,
accents"). Both guards were blind in the same way, so nothing stopped the write.

The rule this patch obeys instead: **the database owns the semantics of a name, so ask the database.**
That is what `taxonomy/grain.py` already says — `same()` casefolds because "MariaDB calls 'GoodFlip'
and 'Goodflip' ONE key". The comparison lives there; nothing here re-decides it.

Three shapes of fold, and each needs a different mechanism:

  * value is already authorised          -> nothing to do.
  * the fold changes the PRIMARY KEY     (`FieldSales` -> `Field-Sales`, `GoodFlip Care` ->
    `Goodflip-Care`, `Pill-Up` -> `Pillup`): a real rename. `rename_doc` handles it, and merges when
    the authorised row already exists.
  * the fold does NOT change the primary key (`TatvaPractice` -> `Tatvapractice`, `GoodFlip` ->
    `Goodflip`): `rename_doc` CANNOT do this. Its UPDATE would collide with the row itself, and if a
    twin exists its merge path throws on the same byte check. These are repointed directly, which is
    safe precisely because the key does not move.

Idempotent and order-free: every step reads the CURRENT spelling and writes the authorised one, so an
interrupted run completes on the next migrate and a second run is a no-op. A fresh site is already in
the authorised shape and this no-ops there too.
"""
import re

import frappe

# THE VOCABULARY. Owner-authorised, 2026-07-27. Every stored grain axis folds onto one of these; a
# value that is not a spelling of one of them (`Nivolumab`, `Insurers`, `Zydus`, ...) is not ours to
# touch and is left exactly as it is.
CANON = (
	"Tatvapractice",
	"Goodflip",
	"Goodflip-Care",
	"Pillup",
	"India",
	"Anaya",
	"Insurers",
	"Field-Sales",
	"Inside-Sales",
	"Niva-Bupa",
	"Liver-Forever",
)

# Grains the owner retired outright. `Goodflip::Insurers::` (blank program) was a partial row beside the
# real one; only the Niva-Bupa grain is valid on that line.
RETIRED_GRAINS = (("Goodflip", "Insurers", ""),)

_MASTERS = ("CRM Vertical", "CRM Group", "CRM Program")
_AXIS_FIELDNAMES = ("vertical", "group", "psp_group", "crm_group", "program")
_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def _squash(value):
	"""The comparison key for 'these are the same word, spelled differently' — case and punctuation
	dropped, so `GoodFlip Care`, `Goodflip-Care` and `goodflipcare` all land together."""
	return re.sub(r"[^a-z0-9]", "", (value or "").lower())


_AUTHORISED = {_squash(c): c for c in CANON}


def authorised(value):
	"""The one authorised spelling of `value`, or `value` untouched if it is not part of the grain
	vocabulary. Never invents: an unknown value is returned as-is so it fails loudly at its Link check
	rather than being silently rewritten into something the masters do not carry."""
	return _AUTHORISED.get(_squash(value), value)


def _same_key(a, b):
	"""Does the DATABASE consider these one primary key? Asked of the database, under the column's own
	collation, because that — not Python's `==` — is what an INSERT or a rename will be judged by."""
	if a == b:
		return True
	return bool(frappe.db.sql("SELECT %s = %s", (a, b))[0][0])


def execute():
	_fold_masters()
	_fold_user_permissions()
	_fold_data_axes()
	_fold_composite_names()
	_drop_retired_grains()
	frappe.db.commit()


# ------------------------------------------------------------------ the masters
def _fold_masters():
	"""Rename each master row onto its authorised spelling. Renaming a master repoints every Link field
	that names it, which is most of the app; the steps after this one clean up what Links cannot reach."""
	for doctype in _MASTERS:
		if not frappe.db.table_exists(doctype):
			continue
		for name in frappe.get_all(doctype, pluck="name"):
			want = authorised(name)
			if want != name:
				_move(doctype, name, want)


# ------------------------------------------------------------------ what rename_doc cannot reach
def _fold_user_permissions():
	"""`User Permission.for_value` is a DYNAMIC link; `get_link_fields` collects `fieldtype='Link'` only,
	so a grain-scoped permission would keep naming a spelling that no longer exists."""
	for allow, for_value in frappe.db.sql(
		"SELECT DISTINCT allow, for_value FROM `tabUser Permission` WHERE allow IN %s", (_MASTERS,)
	):
		want = authorised(for_value)
		if want != for_value:
			frappe.db.sql(
				"UPDATE `tabUser Permission` SET for_value = %s WHERE allow = %s AND BINARY for_value = %s",
				(want, allow, for_value),
			)


def _fold_data_axes():
	"""An axis stored as Data (CRM Picklist Value keeps all three) is invisible to rename_doc."""
	for doctype, fieldname in _data_axis_columns():
		for (value,) in frappe.db.sql(
			f"SELECT DISTINCT `{fieldname}` FROM `tab{doctype}` WHERE `{fieldname}` IS NOT NULL AND `{fieldname}` <> ''"
		):
			want = authorised(value)
			if want != value:
				frappe.db.sql(
					f"UPDATE `tab{doctype}` SET `{fieldname}` = %s WHERE BINARY `{fieldname}` = %s",
					(want, value),
				)


def _data_axis_columns():
	"""(doctype, fieldname) for every Data column that holds a grain axis."""
	rows = frappe.db.sql(
		"""SELECT parent AS dt, fieldname FROM `tabDocField` WHERE fieldtype = 'Data' AND fieldname IN %(axes)s
		   UNION
		   SELECT dt, fieldname FROM `tabCustom Field` WHERE fieldtype = 'Data' AND fieldname IN %(axes)s""",
		{"axes": _AXIS_FIELDNAMES}, as_dict=True,
	)
	return [(r.dt, r.fieldname) for r in rows
	        if frappe.db.table_exists(r.dt) and frappe.db.has_column(r.dt, r.fieldname)]


def _fold_composite_names():
	"""A doctype named `format:{vertical}::{group}::{program}::...` has its Link columns repointed by the
	master rename but its NAME left reading the old spelling — and here the name IS the identity.
	Re-mint from the doctype's own autoname applied to its now-authorised axis fields."""
	for doctype, template in _composite_doctypes():
		keys = _PLACEHOLDER.findall(template)
		for name in frappe.get_all(doctype, pluck="name"):
			doc = frappe.db.get_value(doctype, name, keys, as_dict=True) or {}
			want = template.format(**{k: authorised(doc.get(k) or "") for k in keys})
			if want != name:
				_move(doctype, name, want)
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
			if (df.fieldtype == "Link" and df.options in _MASTERS) or placeholder in _AXIS_FIELDNAMES:
				out.append((doctype, template))
				break
	return out


# ------------------------------------------------------------------ the one move primitive
def _move(doctype, old, new):
	"""Bring row `old` to the name `new`, whatever stands in the way. THE single place a grain rename
	happens, so the collation reasoning is written once.

	Which mechanism applies is decided by ONE question — does this move the primary key? — and that
	question is answered by the database, never by comparing strings in Python."""
	blockers = [n for (n,) in frappe.db.sql(
		f"SELECT name FROM `tab{doctype}` WHERE name = %s AND BINARY name <> %s", (new, old))]

	if _same_key(old, new):
		# A pure case fold: `TatvaPractice` -> `Tatvapractice`. The key does not move, so no INSERT and
		# no UPDATE can collide — but rename_doc still cannot do it (its own UPDATE would target the row
		# it is reading, and its merge path throws on the byte check). Repoint directly.
		if blockers:
			_absorb(doctype, old, blockers[0])          # a twin already holds the key: fold into it
		else:
			_recase(doctype, old, new)
		return

	# A real rename: the key moves. rename_doc is correct here, and merges when the authorised row
	# already exists — but only if that row is BYTE-identical to `new`, which is the check that failed
	# last time. Any blocker that is merely key-equal is folded onto `new` FIRST, so by the time
	# rename_doc runs the target is unambiguous.
	for blocker in blockers:
		if blocker != new:
			_move(doctype, blocker, new)
	exact = frappe.db.sql(f"SELECT 1 FROM `tab{doctype}` WHERE BINARY name = %s", (new,))
	frappe.rename_doc(doctype, old, new, merge=bool(exact), force=True, rebuild_search=False)
	print(f"  normalise_grain_spelling: {doctype} {old!r} -> {new!r}{' (merged)' if exact else ''}")


def _recase(doctype, old, new):
	"""Change only the letters of a name that keeps its primary key. Every reference still resolves
	while this runs — the database already reads them as one value — so the order does not matter."""
	frappe.db.sql(f"UPDATE `tab{doctype}` SET name = %s WHERE BINARY name = %s", (new, old))
	for child in frappe.get_meta(doctype).get_table_fields():
		if frappe.db.table_exists(child.options):
			frappe.db.sql(f"UPDATE `tab{child.options}` SET parent = %s WHERE BINARY parent = %s "
			              f"AND parenttype = %s", (new, old, doctype))
	_repoint_links(doctype, old, new)
	print(f"  normalise_grain_spelling: {doctype} {old!r} -> {new!r} (re-cased in place)")


def _absorb(doctype, loser, keeper):
	"""Two rows spell the same primary key. Only one can survive: move every reference onto `keeper`
	and drop `loser`. Deleted with BINARY so the keeper — which the database reads as the same name —
	is never the row that gets removed."""
	_repoint_links(doctype, loser, keeper)
	for child in frappe.get_meta(doctype).get_table_fields():
		if frappe.db.table_exists(child.options):
			frappe.db.sql(f"DELETE FROM `tab{child.options}` WHERE BINARY parent = %s AND parenttype = %s",
			              (loser, doctype))
	frappe.db.sql(f"DELETE FROM `tab{doctype}` WHERE BINARY name = %s", (loser,))
	print(f"  normalise_grain_spelling: {doctype} {loser!r} absorbed into {keeper!r}")


def _repoint_links(doctype, old, new):
	"""Every Link and Dynamic Link naming `old` now names `new`. Frappe's own collectors are used so
	this covers exactly what a real rename would have covered."""
	from frappe.model.rename_doc import get_link_fields, rename_dynamic_links, update_link_field_values

	update_link_field_values(get_link_fields(doctype), old, new, doctype)
	rename_dynamic_links(doctype, old, new)


# ------------------------------------------------------------------ retirements
def _drop_retired_grains():
	"""A grain the owner struck off. Removed only when it is genuinely empty of leads, so a retirement
	can never be the thing that loses patient data — if it still carries any, it is reported and kept."""
	if not frappe.db.table_exists("CRM Grain"):
		return
	for vertical, group, program in RETIRED_GRAINS:
		for name in frappe.db.sql_list(
			"SELECT name FROM `tabCRM Grain` WHERE vertical = %s AND `group` = %s "
			"AND COALESCE(program, '') = %s", (vertical, group, program)
		):
			leads = frappe.db.count("CRM Lead", {
				"custom_vertical": vertical, "custom_group": group,
				"custom_current_program": program or ["in", ["", None]],
			})
			if leads:
				print(f"  normalise_grain_spelling: KEEPING retired grain {name!r} — {leads} lead(s) still on it")
				continue
			frappe.delete_doc("CRM Grain", name, force=True, ignore_permissions=True)  # authz-ok: tier-a — patch, no caller input
			print(f"  normalise_grain_spelling: retired CRM Grain {name!r}")
