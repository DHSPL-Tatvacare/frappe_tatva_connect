# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""THE filter-preset seam: a person's named filter-and-sort combination on a listing surface.

GENERIC BY CONSTRUCTION, not generalised later. The surface is a Dynamic Link — `(reference_doctype,
reference_name)`, frappe's own way of pointing at anything — so one doctype and one door serve a Smart
View today and the native leads list tomorrow with no new concept. A blank `reference_name` means the
whole list of that doctype, which is exactly what a native list view is.

WHY NOT `CRM View Settings`. Crm's own saved views are scoped by `dt` alone, and `crm.api.views.get_views`
returns every row for that `dt`. Storing a preset there is invisible only while `dt` is a value no native
list asks for; the moment presets are wanted ON the leads list, `dt` must be `CRM Lead` and they land in
the native view dropdown beside real views. Genericity and that table are in direct conflict.

THE GATE IS THE SURFACE'S OWN. You may keep a preset on anything you can open — `frappe.has_permission`
on the referenced document. For a Smart View that routes through `smartview.permissions`'s own hook, for
a CRM Lead through the row gate; this module states no rule of its own and holds no per-surface case.

PERSONAL, FULL STOP. `user` is always the session, never a parameter, so there is no sharing to gate and
no public flag to get wrong. The surface a preset sits on carries its own sharing rules.

A PRESET REMEMBERS AN ANSWER, IT DOES NOT KNOW WHAT THE ANSWER MEANS. `filters` and `sort` are stored and
returned as the caller's own shapes; nothing here parses them, so a surface may change its filter grammar
without this module learning a second one.
"""
import frappe
from frappe import _
from frappe.utils import cstr

DOCTYPE = "CRM Filter Preset"

# How many presets one person may keep on one surface. A dropdown is a shortlist; past this it is a list
# view of its own, and the person wanted a Smart View rather than a hundred presets.
PER_SURFACE_CAP = 20


def may_use(reference_doctype, reference_name=None):
	"""THE gate, asked rather than thrown: may this person open the surface a preset sits on.

	A reader that LISTS presets needs the verdict and a writer needs the refusal, so the question is
	written once here and `_assert_may_use` is the same answer with a throw on it."""
	return bool(reference_doctype) and bool(
		frappe.has_permission(reference_doctype, "read", doc=reference_name or None)
	)


def _assert_may_use(reference_doctype, reference_name=None):
	"""The one gate: you may keep presets on a surface you can open. Fail-closed."""
	if not reference_doctype:
		frappe.throw(_("A preset needs a surface."))
	if not may_use(reference_doctype, reference_name):
		frappe.throw(_("Not permitted."), frappe.PermissionError)


def _mine(reference_doctype, reference_name=None):
	return {
		"user": frappe.session.user,
		"reference_doctype": reference_doctype,
		"reference_name": cstr(reference_name or ""),
	}


@frappe.whitelist()
@frappe.read_only()
def list_presets(reference_doctype, reference_name=None):
	"""This person's presets on one surface, newest first. Read-only; never anyone else's."""
	_assert_may_use(reference_doctype, reference_name)
	return frappe.get_all(  # authz-ok: tier-a — this seam's own rows, filtered to the session user
		DOCTYPE,
		filters={**_mine(reference_doctype, reference_name), "is_current": 0},
		fields=["name", "label", "filters", "sort"],
		order_by="modified desc",
		limit_page_length=PER_SURFACE_CAP,
		ignore_permissions=True,
	)


def _store(doc, filters, sort):
	"""The ONE way this module writes the pair, so a named preset and the current state cannot diverge."""
	doc.filters = frappe.as_json(frappe.parse_json(filters) if isinstance(filters, str) else (filters or []))
	doc.sort = frappe.as_json(frappe.parse_json(sort) if isinstance(sort, str) else sort) if sort else None
	doc.save(ignore_permissions=True)  # authz-ok: tier-a — this seam's own row, gated by the caller, owned by the session user
	return doc


@frappe.whitelist()
@frappe.read_only()
def current(reference_doctype, reference_name=None):
	"""What this person last had applied on one surface, or None — the state a refresh must not lose.

	The same pair a preset holds and the same door it comes through; the only difference is that nobody
	named it. Returned in the shape `list_presets` returns, so a caller applies both the same way."""
	_assert_may_use(reference_doctype, reference_name)
	rows = frappe.get_all(  # authz-ok: tier-a — this seam's own row, filtered to the session user
		DOCTYPE,
		filters={**_mine(reference_doctype, reference_name), "is_current": 1},
		fields=["name", "label", "filters", "sort"],
		limit_page_length=1,
		ignore_permissions=True,
	)
	return rows[0] if rows else None


@frappe.whitelist()
def remember_current(reference_doctype, filters=None, sort=None, reference_name=None):
	"""Keep what is applied RIGHT NOW, so reopening the surface reopens the question.

	One row per person per surface, overwritten as they work — it is a cursor, not a history, so it is
	never capped and never listed. `PER_SURFACE_CAP` counts what they chose to name."""
	_assert_may_use(reference_doctype, reference_name)
	keys = {**_mine(reference_doctype, reference_name), "is_current": 1}
	existing = frappe.db.exists(DOCTYPE, keys)
	doc = frappe.get_doc(DOCTYPE, existing) if existing else frappe.new_doc(DOCTYPE).update(
		{**keys, "label": _("Current")}
	)
	_store(doc, filters, sort)
	return {"name": doc.name, "filters": doc.filters, "sort": doc.sort}


@frappe.whitelist()
def save_preset(reference_doctype, label, filters=None, sort=None, reference_name=None):
	"""Create or overwrite one preset by its label — saving the same name twice is the person correcting
	themselves, not a second row they now have to tidy up."""
	_assert_may_use(reference_doctype, reference_name)
	label = cstr(label).strip()
	if not label:
		frappe.throw(_("Give the preset a name."))
	keys = _mine(reference_doctype, reference_name)
	existing = frappe.db.exists(DOCTYPE, {**keys, "label": label})
	if not existing and frappe.db.count(DOCTYPE, {**keys, "is_current": 0}) >= PER_SURFACE_CAP:
		frappe.throw(_("You already have {0} presets here. Delete one first.").format(PER_SURFACE_CAP))
	doc = frappe.get_doc(DOCTYPE, existing) if existing else frappe.new_doc(DOCTYPE).update({**keys, "label": label})
	_store(doc, filters, sort)
	return {"name": doc.name, "label": doc.label, "filters": doc.filters, "sort": doc.sort}


@frappe.whitelist()
def delete_preset(name):
	"""Delete one of YOUR presets. Someone else's is not found rather than refused — a preset is personal,
	so its existence is not a fact this caller is entitled to."""
	row = frappe.db.get_value(DOCTYPE, name, ["user", "reference_doctype", "reference_name"], as_dict=True)
	if not row or row.user != frappe.session.user:
		frappe.throw(_("No such preset."), frappe.DoesNotExistError)
	_assert_may_use(row.reference_doctype, row.reference_name)
	frappe.delete_doc(DOCTYPE, name, ignore_permissions=True)  # authz-ok: tier-a — the caller's own row, gated above
	return {"deleted": name}
