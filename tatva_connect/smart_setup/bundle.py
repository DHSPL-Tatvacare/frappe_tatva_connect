# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Smart Setup's engine: collect a recipe's records into a bundle on one site, check or apply it on another.

A bundle is Frappe's own document JSON, one record per setup row with its section rows inline, wrapped in an
envelope that names its recipe and roots. Records match across sites by `name`, which a setup row derives from
its own fields (`field:` and `format:` naming), so UAT and production agree on it. A doctype with an engine of
its own (a workflow) is read and written through that engine: `doctypes.ADAPTERS`.

Check and apply are one path. Records are written in dependency order through the normal save (or the
doctype's own engine), so every rule runs. A missing record is created; an existing one is updated only if the
operator picked it (a root), and any other is kept as this site has it. Before each write, every record it points
at must be here, or the record is refused naming what it needs. Check rolls everything back; apply commits only
when nothing was refused, so a setup lands whole or not at all.

A restore point is a bundle too: the versions an apply replaced (`records`), what each must still be for the
restore to write it back (`expected`), and what the apply created (`remove`). It is checked and applied the same way.
"""
from collections import Counter
from copy import deepcopy
from functools import partial
from graphlib import TopologicalSorter

import frappe
from frappe import _
from frappe.model import child_table_fields, default_fields, optional_fields
from frappe.utils import cstr, now, strip_html
from frappe.utils.messages import get_message_log

from tatva_connect.smart_setup import doctypes, recipes
from tatva_connect.taxonomy import labels

FORMAT = "tatva-smart-setup"
VERSION = 1
CREATED, UPDATED, UNCHANGED, KEPT, REMOVED, REFUSED = "created", "updated", "unchanged", "kept", "removed", "refused"
ACTIONS = (CREATED, UPDATED, UNCHANGED, KEPT, REMOVED, REFUSED)
_PARTS = ("records", "expected", "remove")  # what to write, what each must still be, what to remove

# Frappe's own bookkeeping fields describe this copy of a record, not the setup, so no bundle carries them.
_RECORD_DROP = (set(default_fields) | set(optional_fields)) - {"doctype", "name"}
_ROW_DROP = set(default_fields) | set(optional_fields) | set(child_table_fields)
_SAVEPOINT = "smart_setup_record"


# -- the bundle ----------------------------------------------------------------

def build(recipe_key, roots):
	"""The bundle for one recipe's roots, as the JSON text a file holds."""
	return _envelope(recipe_key, list(roots), records=collect(recipe_key, roots))


def collect(recipe_key, roots):
	"""The recipe's records reachable from its roots, each after every record it links to."""
	recipe = recipes.get(recipe_key)
	records = {}
	pending = [(recipe["root"], root) for root in roots]
	while pending:
		key = pending.pop()
		if key in records:
			continue
		records[key] = record = _adapter(key[0]).read(*key)
		pending += [link for link in _links(record) if link[0] in recipe["carries"] and link not in records]
		vocabulary = doctypes.VOCABULARY.get(key[0])
		if vocabulary and labels.PICKLIST_VALUE in recipe["carries"]:
			pending += [link for link in vocabulary(record) if link not in records]
	for doctype in recipe["companions"]:
		for name in _companions(doctype, records):
			records[(doctype, name)] = _adapter(doctype).read(doctype, name)
	return [records[key] for key in _order(records)]


def read(text):
	"""A bundle this site can apply, or a refusal saying why it cannot."""
	try:
		bundle = frappe.parse_json(text)
	except ValueError:
		bundle = None
	if not isinstance(bundle, dict) or bundle.get("format") != FORMAT:
		frappe.throw(_("This file is not a Smart Setup bundle. Attach the file a Smart Setup export produced."),
		             title=_("Not a Smart Setup bundle"))
	if bundle.get("version") != VERSION:
		frappe.throw(_("This bundle is version {0} and this site reads version {1}. Export it again from a site "
		               "running the same release.").format(bundle.get("version"), VERSION), title=_("Wrong version"))
	allowed = recipes.doctypes(recipes.get(bundle.get("recipe")))
	for part in _PARTS:
		for record in bundle.get(part) or []:
			if record.get("doctype") not in allowed:
				frappe.throw(_("This bundle holds a {0}, which the {1} recipe does not carry.").format(
					record.get("doctype"), bundle["recipe"]), title=_("Record outside the recipe"))
			_refuse_secrets(frappe.get_meta(record["doctype"]), record)
		bundle[part] = [_strip(record) for record in bundle.get(part) or []]  # a hand-edited file names no owner
	return bundle


def check(bundle, progress=None):
	"""What applying the bundle would do to each record, found by applying it and rolling it all back."""
	return _run(bundle, commit=False, progress=progress)


def apply(bundle, progress=None, before_commit=None):
	"""Apply every record or none; `before_commit(results)` writes in the same transaction, landing with it or not."""
	return _run(bundle, commit=True, progress=progress, before_commit=before_commit)


def versions(bundle):
	"""This site's version of every record the bundle names, read before an apply writes any of them."""
	keys = {_key(record) for part in _PARTS for record in bundle.get(part) or []}
	return {key: _adapter(key[0]).read(*key) for key in keys if frappe.db.exists(*key)}


def restore_point(bundle, before, results):
	"""The bundle that puts back what `results` changed: prior versions, creates to remove, removals to recreate."""
	done = {action: [(r["ref_doctype"], r["record"]) for r in results if r["action"] == action] for action in ACTIONS}
	now_version = {key: _adapter(key[0]).read(*key) for key in done[UPDATED] + done[CREATED]}
	return _envelope(bundle["recipe"], [name for _doctype, name in done[UPDATED]],
	                 records=[before[key] for key in done[UPDATED] + done[REMOVED]],
	                 expected=[now_version[key] for key in done[UPDATED]],
	                 remove=[now_version[key] for key in done[CREATED]])


def size(bundle):
	"""How many records a bundle acts on: those it writes and those it removes (an expected version acts on none)."""
	return len(bundle.get("records") or []) + len(bundle.get("remove") or [])


def same(a, b):
	"""Two records equal as frappe reads them: a blank field is blank whether it was stored NULL or ''."""
	return _blank_as_none(a) == _blank_as_none(b)


def _envelope(recipe_key, roots, **parts):
	return frappe.as_json({"format": FORMAT, "version": VERSION, "recipe": recipe_key, "roots": roots,
	                       "source_site": frappe.local.site, "exported_at": now(), **parts})


# -- check and apply -----------------------------------------------------------

def _run(bundle, commit, progress, before_commit=None):
	records = bundle["records"]
	bundled = {_key(record) for record in records}
	roots = {(recipes.get(bundle["recipe"])["root"], name) for name in bundle["roots"]}
	expected = {_key(record): record for record in bundle.get("expected") or []}
	steps = [(record, partial(_write, record, bundled, roots, expected)) for record in records]
	steps += [(record, partial(_remove, record)) for record in reversed(bundle.get("remove") or [])]  # dependents first
	results = []
	for i, (record, step) in enumerate(steps, 1):
		frappe.db.savepoint(_SAVEPOINT)
		try:
			action, message = step()
		# A record the target refuses is that record's verdict, in frappe's own sentence or the need it names.
		except Exception as e:
			frappe.db.rollback(save_point=_SAVEPOINT)
			action, message = REFUSED, _told(e)
			if not isinstance(e, frappe.ValidationError):
				# A fault on our side, not the record's: deferred, so the rollback that ends a check cannot drop it.
				frappe.log_error(title=f"Smart Setup could not write {record['doctype']} {record['name']}",
				                 reference_doctype=record["doctype"], reference_name=record["name"], defer_insert=True)
				message = _("An error on this site stopped this record; it is in the Error Log. {0}").format(message)
			frappe.clear_messages()
		results.append({"ref_doctype": record["doctype"], "record": record["name"], "action": action,
		                "message": message})
		if progress:
			progress(i, len(steps))
	if commit and not any(r["action"] == REFUSED for r in results):
		if before_commit:
			before_commit(results)
		frappe.db.commit()
	else:
		frappe.db.rollback()
	return results


def _write(record, bundled, roots, expected):
	"""(action, message) for one record: created if missing, updated only if the operator picked it, else left as it is."""
	key = _key(record)
	adapter = _adapter(key[0])
	current = adapter.read(*key) if frappe.db.exists(*key) else None
	if current is not None and same(current, record):
		return UNCHANGED, ""
	if key in expected and (current is None or not same(current, expected[key])):
		_refuse_changed_since()
	if current is not None and key not in roots:
		return KEPT, _("Differs from the bundle; this site's version is kept.")
	_assert_links(record, bundled)
	adapter.write(deepcopy(record), current is not None)  # frappe writes into the dicts it is handed
	return (UPDATED, _changes(current, record)) if current is not None else (CREATED, "")


def _remove(record):
	"""(action, message) for a record a restore takes back: removed if this site still holds it as the apply left it."""
	key = _key(record)
	if not frappe.db.exists(*key):
		return UNCHANGED, _("Already gone from this site.")
	if not same(_adapter(key[0]).read(*key), record):
		_refuse_changed_since()
	_adapter(key[0]).remove(*key)
	return REMOVED, ""


def _assert_links(record, bundled):
	"""Every record `record` links to is on this site; each missing one is named in one sentence, whatever its doctype."""
	needs = sorted(link for link in _links(record) if not frappe.db.exists(*link))
	if needs:
		frappe.throw("; ".join((_("Needs {0} {1}, which this bundle could not write.") if link in bundled
		                        else _("Needs {0} {1}: set it up on this site first.")).format(*link) for link in needs))


def _refuse_changed_since():
	frappe.throw(_("Changed on this site after the apply; restore it by hand."))


def _changes(current, record):
	"""What an update changes, in words: fields by label, and rows added and removed per table."""
	meta = frappe.get_meta(record["doctype"])
	fields, tables = [], []
	for key in sorted(set(current) | set(record)):
		old, new = _blank_as_none(current.get(key)), _blank_as_none(record.get(key))
		if old == new:
			continue
		label = _(meta.get_label(key)) if meta.get_field(key) else frappe.unscrub(key)
		if isinstance(old, list) or isinstance(new, list):  # by content: version.get_diff pairs rows by a per-site name
			before, after = Counter(map(frappe.as_json, old or [])), Counter(map(frappe.as_json, new or []))
			tables.append(_("{0}: {1} added, {2} removed").format(label, (after - before).total(), (before - after).total()))
		else:
			fields.append(label)
	return "; ".join(([_("Changes: {0}").format(", ".join(fields))] if fields else []) + tables)


def _told(error):
	"""Everything frappe said refusing a record (a delete says why, then how), as plain text for a grid cell."""
	said = " ".join(cstr(m.get("message")) for m in get_message_log()).strip()
	return strip_html(said or cstr(error)) or type(error).__name__


# -- the document adapter: any doctype without an engine of its own ------------

def _document_read(doctype, name):
	"""One record as Frappe's document JSON, as the operator may read it, minus the fields that describe this copy."""
	doc = frappe.get_doc(doctype, name)
	doc.check_permission("read")
	_refuse_secrets(doc.meta, doc)
	return _strip(doc.as_dict(convert_dates_to_str=True))


def _document_links(record):
	"""(doctype, name) of every Link the record and its section rows hold, read off the schema."""
	meta = frappe.get_meta(record["doctype"])
	rows = [(meta, record)] + [(frappe.get_meta(df.options), row) for df in meta.get_table_fields()
	                           for row in record.get(df.fieldname) or []]
	return {(df.options, row.get(df.fieldname)) for row_meta, row in rows for df in row_meta.get_link_fields()
	        if row.get(df.fieldname)}


def _document_write(record, exists):
	"""The normal save: insert a new record, or update the existing one to match, so every rule runs."""
	if not exists:
		frappe.get_doc(record).insert(set_name=record["name"])  # identity is the source's name, whatever naming spells today
		return
	doc = frappe.get_doc(record["doctype"], record["name"])
	doc.update(record)
	doc.save()


def _document_remove(doctype, name):
	"""Frappe's own delete: permission-checked, and refused while any record still links to this one."""
	frappe.delete_doc(doctype, name)


_DOCUMENT = doctypes.Adapter(_document_read, _document_links, _document_write, _document_remove)


def _adapter(doctype):
	return doctypes.ADAPTERS.get(doctype) or _DOCUMENT


def _links(record):
	return _adapter(record["doctype"]).links(record)


# -- a record's shape ----------------------------------------------------------

def _key(record):
	return (record["doctype"], record["name"])


def _strip(record):
	"""A record as a bundle holds it: Frappe's bookkeeping dropped from it and from the section rows it carries."""
	out = {k: v for k, v in record.items() if k not in _RECORD_DROP}
	for df in frappe.get_meta(record["doctype"]).get_table_fields():
		if df.fieldname in out:
			out[df.fieldname] = [{k: v for k, v in row.items() if k not in _ROW_DROP} for row in out[df.fieldname] or []]
	return out


def _refuse_secrets(meta, record):
	"""A bundle never carries a secret: a record holding a Password value is refused, never stripped."""
	for df in meta.get("fields", {"fieldtype": "Password"}):
		if record.get(df.fieldname):
			frappe.throw(_("{0} {1} holds a password in {2}, and a Smart Setup bundle never carries one.").format(
				meta.name, record.get("name"), df.label), title=_("Secret in bundle"))


def _blank_as_none(value):
	if isinstance(value, dict):
		return {k: _blank_as_none(v) for k, v in value.items()}
	if isinstance(value, list):
		return [_blank_as_none(v) for v in value]
	return None if value == "" else value


# -- the walk ------------------------------------------------------------------

def _companions(doctype, records):
	"""Names of `doctype` rows whose every Link lands in the bundle, or on another such row."""
	links = frappe.get_meta(doctype).get_link_fields()
	rows = frappe.get_all(doctype, fields=["name", *(df.fieldname for df in links)])
	kept = {r.name: r for r in rows if any(r.get(df.fieldname) for df in links)}
	while True:
		stray = [name for name, r in kept.items()
		         if any(r.get(df.fieldname) and (df.options, r.get(df.fieldname)) not in records
		                and not (df.options == doctype and r.get(df.fieldname) in kept) for df in links)]
		if not stray:
			return list(kept)
		for name in stray:
			kept.pop(name)


def _order(records):
	"""Bundle keys with every record after the records it links to, so an apply never meets a dangling Link."""
	return list(TopologicalSorter(
		{key: {link for link in _links(record) if link in records and link != key} for key, record in records.items()}
	).static_order())
