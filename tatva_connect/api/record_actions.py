# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The one door to record actions, for every app's record header and sidebar: what a record offers, an action's form,
running it, and what earlier runs made. Each call asks the record's own permission first."""
import frappe

from tatva_connect import record_actions


@frappe.whitelist(methods=["GET"])
def actions(doctype, name):
	"""The actions this record offers the caller now."""
	frappe.has_permission(doctype, "read", name, throw=True)
	return [record_actions.card(action) for action in record_actions.offered(doctype)]


@frappe.whitelist(methods=["GET"])
def form(action, doctype, name):
	"""The dialog for one action on this record: its title, its submit label and its fields with their defaults."""
	doc = _writable(doctype, name)
	return record_actions.get(action, doctype).form(doc)


@frappe.whitelist(methods=["POST"])
def run(action, doctype, name, values=None):
	"""Run one action on this record and keep what it made; returns `{key, url}`."""
	doc = _writable(doctype, name)
	chosen = record_actions.get(action, doctype)
	made = chosen.run(doc, frappe.parse_json(values) or {})
	record_actions.remember(chosen, doc, made)
	return made


@frappe.whitelist(methods=["GET"])
def links(doctype, name):
	"""What earlier runs made for this record, each with its live state from the other system."""
	frappe.has_permission(doctype, "read", name, throw=True)
	return [record_actions.view(link) for link in record_actions.links_of(doctype, name)]


def _writable(doctype, name):
	"""The record, once the caller may write it: acting on a record is a change to it."""
	doc = frappe.get_doc(doctype, name)
	doc.check_permission("write")
	return doc
