# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Smart Setup's engine: collect a recipe's records into a bundle on one site, check or apply it on another.

A bundle is Frappe's own document JSON, one record per setup row with its section rows inline, wrapped in an
envelope that names its recipe and roots. Records match across sites by `name`, which a setup row derives from
its own fields (`field:` and `format:` naming), so UAT and production agree on it. A doctype with an engine of
its own (a workflow) is read and written through that engine: `doctypes.ADAPTERS`.

Check and apply are one path. Records are written in dependency order through the normal save (or the
doctype's own engine), so every rule runs. Before each write, every record it points at outside the bundle
must exist here, or the record is refused naming what it needs. Check rolls everything back; apply commits
only when nothing was refused, so a setup lands whole or not at all.
"""
from copy import deepcopy
from graphlib import TopologicalSorter

import frappe
from frappe import _
from frappe.model import child_table_fields, default_fields, optional_fields
from frappe.utils import cstr, now, strip_html

from tatva_connect.smart_setup import doctypes, recipes
from tatva_connect.taxonomy import labels

FORMAT = "tatva-smart-setup"
VERSION = 1
CREATED, UPDATED, UNCHANGED, REFUSED = "created", "updated", "unchanged", "refused"
ACTIONS = (CREATED, UPDATED, UNCHANGED, REFUSED)

# Frappe's own bookkeeping fields describe this copy of a record, not the setup, so no bundle carries them.
_RECORD_DROP = (set(default_fields) | set(optional_fields)) - {"doctype", "name"}
_ROW_DROP = set(default_fields) | set(optional_fields) | set(child_table_fields)
_SAVEPOINT = "smart_setup_record"


def build(recipe_key, roots):
	"""The bundle for one recipe's roots, as the JSON text a file holds."""
	return frappe.as_json({
		"format": FORMAT, "version": VERSION, "recipe": recipe_key, "roots": list(roots),
		"source_site": frappe.local.site, "exported_at": now(), "records": collect(recipe_key, roots),
	})


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
	for record in bundle.get("records") or []:
		if record.get("doctype") not in allowed:
			frappe.throw(_("This bundle holds a {0}, which the {1} recipe does not carry.").format(
				record.get("doctype"), bundle["recipe"]), title=_("Record outside the recipe"))
		_refuse_secrets(frappe.get_meta(record["doctype"]), record)
	bundle["records"] = [_strip(record) for record in bundle.get("records") or []]  # a hand-edited file names no owner or docstatus
	return bundle


def check(bundle, progress=None):
	"""What applying the bundle would do to each record, found by applying it and rolling it all back."""
	return _run(bundle, commit=False, progress=progress)


def apply(bundle, progress=None):
	"""Apply every record, or none: one refused record rolls the whole bundle back."""
	return _run(bundle, commit=True, progress=progress)


def same(a, b):
	"""Two records equal as frappe reads them: a blank field is blank whether it was stored NULL or ''."""
	return _blank_as_none(a) == _blank_as_none(b)


def _run(bundle, commit, progress):
	records = bundle["records"]
	bundled = {(record["doctype"], record["name"]) for record in records}
	results = []
	for i, record in enumerate(records, 1):
		frappe.db.savepoint(_SAVEPOINT)
		try:
			action, message = _write(record, bundled), ""
		# A record the target refuses is that record's verdict, in frappe's own sentence or the need it names.
		except Exception as e:
			frappe.db.rollback(save_point=_SAVEPOINT)
			action, message = REFUSED, strip_html(cstr(e)) or type(e).__name__  # a verdict is read in a grid cell, not a dialog
			if not isinstance(e, frappe.ValidationError):
				# A fault on our side, not the record's: deferred, so the rollback that ends a check cannot drop it.
				frappe.log_error(title=f"Smart Setup could not write {record['doctype']} {record['name']}",
				                 reference_doctype=record["doctype"], reference_name=record["name"], defer_insert=True)
				message = _("An error on this site stopped this record; it is in the Error Log. {0}").format(message)
			frappe.clear_messages()
		results.append({"ref_doctype": record["doctype"], "record": record["name"], "action": action,
		                "message": message})
		if progress:
			progress(i, len(records))
	if commit and not any(r["action"] == REFUSED for r in results):
		frappe.db.commit()
	else:
		frappe.db.rollback()
	return results


def _write(record, bundled):
	"""Create the record, update it to match, or leave it, after checking every record it links to is on this site."""
	needs = sorted(link for link in _links(record) if not frappe.db.exists(*link))
	if needs:
		frappe.throw("; ".join((_("Needs {0} {1}, which this bundle could not write.") if link in bundled
		                        else _("Needs {0} {1}: set it up on this site first.")).format(*link) for link in needs))
	adapter = _adapter(record["doctype"])
	exists = bool(frappe.db.exists(record["doctype"], record["name"]))
	if exists and same(adapter.read(record["doctype"], record["name"]), record):
		return UNCHANGED
	adapter.write(deepcopy(record), exists)  # frappe writes into the dicts it is handed; apply reads the bundle again
	return UPDATED if exists else CREATED


def _adapter(doctype):
	return doctypes.ADAPTERS.get(doctype) or _DOCUMENT


def _document_read(doctype, name):
	"""One record as Frappe's document JSON, as the operator may read it, minus the fields that describe this copy of it."""
	doc = frappe.get_doc(doctype, name)
	doc.check_permission("read")
	_refuse_secrets(doc.meta, doc)
	return _strip(doc.as_dict(convert_dates_to_str=True))


def _strip(record):
	"""A record as a bundle holds it: Frappe's bookkeeping dropped from it and from the section rows it carries."""
	out = {k: v for k, v in record.items() if k not in _RECORD_DROP}
	for df in frappe.get_meta(record["doctype"]).get_table_fields():
		if df.fieldname in out:
			out[df.fieldname] = [{k: v for k, v in row.items() if k not in _ROW_DROP} for row in out[df.fieldname] or []]
	return out


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
		frappe.get_doc(record).insert(set_name=record["name"])  # identity is the source's name, even where today's naming would spell it differently
		return
	doc = frappe.get_doc(record["doctype"], record["name"])
	doc.update(record)
	doc.save()


_DOCUMENT = doctypes.Adapter(_document_read, _document_links, _document_write)


def _links(record):
	return _adapter(record["doctype"]).links(record)


def _refuse_secrets(meta, record):
	"""A bundle never carries a secret: a record holding a Password value is refused, never stripped."""
	for df in meta.get("fields", {"fieldtype": "Password"}):
		if record.get(df.fieldname):
			frappe.throw(_("{0} {1} holds a password in {2}, and a Smart Setup bundle never carries one.").format(
				meta.name, record.get("name"), df.label), title=_("Secret in bundle"))


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


def _blank_as_none(value):
	if isinstance(value, dict):
		return {k: _blank_as_none(v) for k, v in value.items()}
	if isinstance(value, list):
		return [_blank_as_none(v) for v in value]
	return None if value == "" else value
