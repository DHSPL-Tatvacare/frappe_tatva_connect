"""Who is editing a Workflow or a Task Form: an advisory mark the editors show, counted only while its holder is viewing.

The guarantee is not here: a second editor's save is refused by the `modified` check each save already makes."""
import frappe

# One redis hash for every authored record, `{"<doctype>::<name>": user}`; no schema, and a cache flush only forgets a mark.
_KEY = "authoring_editor"

# The realtime event the editors listen for on the record's own doc room.
EVENT = "authoring_editor"


def _field(doctype, name):
	return f"{doctype}::{name}"


def editor(doctype, name):
	"""The user who last started editing this record, or None; rides the payload each editor already loads."""
	return frappe.cache.hget(_KEY, _field(doctype, name))


def start(doctype, name):
	"""The caller starts editing: mark them and tell everyone viewing the record."""
	frappe.has_permission(doctype, "write", name, throw=True)
	frappe.cache.hset(_KEY, _field(doctype, name), frappe.session.user)
	_announce(doctype, name, frappe.session.user)


def stop(doctype, name):
	"""The caller stops editing: clear the mark only if it is theirs, and tell everyone viewing the record."""
	frappe.has_permission(doctype, "write", name, throw=True)
	if editor(doctype, name) != frappe.session.user:
		return
	frappe.cache.hdel(_KEY, _field(doctype, name))
	_announce(doctype, name, None)


def _announce(doctype, name, user):
	"""To the record's doc room only, which Frappe admits a socket to only if it may read the record."""
	frappe.publish_realtime(EVENT, {"doctype": doctype, "name": name, "user": user}, doctype=doctype, docname=name)
