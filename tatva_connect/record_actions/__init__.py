# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Record actions: the closed list of things a person can do to a record from its header, and what each run made for it.

An action is a module that declares `KEY`, `LABEL`, `LABEL_MADE`, `ICON`, `SECTION` and `DOCTYPES` ({doctype: what a
person calls it}) and answers `ready(doctype)`, `form(doc, values)`, `search(doc, field, query, values)`, `refresh()`,
`run(doc, values)`, `live(link, fresh)` and `on_update(doc)`. `api/record_actions.py` is the only door, and asks the record's
own permission before anything here runs.

A form field is `{fieldname, label, fieldtype, reqd, default, options, half, reloads}`: `fieldtype` is Data, Small Text,
Select, Date, Search (one value, searched on the server) or Tags (many values; searched when `searchable`, new ones when
`allow_new`); an option is `{label, value, image}`; `reloads` asks the server for the form again when the field changes.
"""
import frappe
from frappe import _

from tatva_connect.record_actions import jira

LINK_DT = "Tatva Record Action Link"
# The closed list: an action not named here cannot be offered or run.
ACTIONS = {jira.KEY: jira}


def offered(doctype):
	"""The actions a record of `doctype` offers now: each serves the doctype, is switched on and is set up for it."""
	return [action for action in ACTIONS.values() if doctype in action.DOCTYPES and action.ready(doctype)]


def get(key, doctype):
	"""The action `key` for a record of `doctype`, or a refusal naming why it cannot run."""
	action = ACTIONS.get(key)
	if not action or action not in offered(doctype):
		frappe.throw(_("This action is not available for this record."), title=_("Not available"))
	return action


def card(action):
	return {"key": action.KEY, "label": _(action.LABEL), "icon": action.ICON}


def remember(action, doc, made):
	"""Keep what a run made for the record, and say so on its timeline."""
	frappe.get_doc({
		"doctype": LINK_DT, "action": action.KEY, "reference_doctype": doc.doctype, "reference_name": doc.name,
		"external_key": made["key"], "external_url": made["url"],
	}).insert(ignore_permissions=True)  # authz-ok: tier-b — the API checked write on the record first
	doc.add_comment("Info", _("created {0} {1}").format(_(action.LABEL_MADE), made["key"]))


def links_of(doctype, name, action_key=None):
	"""What earlier runs made for one record, newest first; one action's only when `action_key` is given."""
	filters = {"reference_doctype": doctype, "reference_name": name, **({"action": action_key} if action_key else {})}
	return frappe.get_all(  # authz-ok: tier-b — the API checked read on the record first
		LINK_DT, filters=filters, fields=["name", "action", "external_key", "external_url", "creation"], order_by="creation desc")


def view(link, fresh=False):
	"""One link as the sidebar draws it: what it is, where it opens, and what its action reads live (`fresh` skips its cache)."""
	action = ACTIONS.get(link.action)
	live = action.live(link, fresh) if action and action.ready() else {}
	return {"key": link.external_key, "url": link.external_url, "section": _(action.SECTION) if action else link.action,
	        "icon": action.ICON if action else None, **live}


def on_update(doc, method=None):
	"""doc_events on_update: each ready action that serves the doctype follows the change, such as a ticket closing."""
	for action in ACTIONS.values():
		if doc.doctype in action.DOCTYPES and action.ready():
			action.on_update(doc)
