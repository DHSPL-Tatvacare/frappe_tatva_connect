# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The one door to record actions, for every app's record header and sidebar: what a record offers, an action's form,
its searches, refreshing what it remembers, running it, and what earlier runs made. Each call asks the record's own
permission first."""
import frappe

from tatva_connect import record_actions


@frappe.whitelist(methods=["GET"])
def actions(doctype, name):
	"""The actions this record offers the caller now."""
	frappe.has_permission(doctype, "read", name, throw=True)
	return [record_actions.card(action) for action in record_actions.offered(doctype)]


@frappe.whitelist(methods=["GET"])
def form(action, doctype, name, values=None):
	"""The dialog for one action on this record, for the choices made so far: title, submit label, fields, and what
	earlier runs already made for it."""
	doc = _writable(doctype, name)
	chosen = record_actions.get(action, doctype)
	return {**chosen.form(doc, frappe.parse_json(values) or {}),
	        "existing": [record_actions.view(link) for link in record_actions.links_of(doctype, name, chosen.KEY)]}


@frappe.whitelist(methods=["GET"])
def search(action, doctype, name, field, query="", values=None):
	"""Options for one searched field of the dialog, matching what the person typed."""
	doc = _writable(doctype, name)
	return record_actions.get(action, doctype).search(doc, field, (query or "").strip(), frappe.parse_json(values) or {})


@frappe.whitelist(methods=["POST"])
def refresh(action, doctype, name):
	"""Forget what the action remembers from the other system, so the dialog reads it afresh."""
	_writable(doctype, name)
	record_actions.get(action, doctype).refresh()


@frappe.whitelist(methods=["POST"])
def run(action, doctype, name, values=None):
	"""Run one action on this record and keep what it made; returns `{key, url}`."""
	doc = _writable(doctype, name)
	chosen = record_actions.get(action, doctype)
	made = chosen.run(doc, frappe.parse_json(values) or {})
	record_actions.remember(chosen, doc, made)
	return made


@frappe.whitelist(methods=["GET"])
def links(doctype, name, fresh=0):
	"""What earlier runs made for this record, each with its live state from the other system; `fresh` skips the cache."""
	frappe.has_permission(doctype, "read", name, throw=True)
	return [record_actions.view(link, frappe.utils.cint(fresh)) for link in record_actions.links_of(doctype, name)]


def _writable(doctype, name):
	"""The record, once the caller may write it: acting on a record is a change to it."""
	doc = frappe.get_doc(doctype, name)
	doc.check_permission("write")
	return doc
