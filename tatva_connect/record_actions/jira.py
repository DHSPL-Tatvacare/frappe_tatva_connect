# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Raise a Jira issue from a ticket or a lead (Jira Cloud REST v3): the form, the issue, the record as a link inside
Jira, and the issue's live status for the sidebar. No patient detail is sent: the summary and note are the person's,
and the record travels as its link."""
import re

import frappe
from frappe import _
from frappe.integrations.utils import make_request
from frappe.utils import get_fullname

from tatva_connect import automation
from tatva_connect.automation import subjects
from tatva_connect.notifications import deep_link
from tatva_connect.taxonomy import grain

KEY = "jira_issue"
LABEL = "Raise Jira issue"
LABEL_MADE = "Jira issue"
SECTION = "Engineering issue"
ICON = "/assets/tatva_connect/images/jira.jpeg"
# What a person calls each record this action serves.
DOCTYPES = {"HD Ticket": "Ticket", "CRM Lead": "Lead"}
SETTINGS = "Tatva Record Actions"
SWITCH = "Jira::Issues::raise"
# Seconds a Jira call may take before the person is told it did not answer.
TIMEOUT = 20
# The issue fields the sidebar card reads.
LIVE_FIELDS = "summary,status,assignee,priority,updated"


class JiraError(Exception):
	"""Jira refused or did not answer; the message is the person's to read."""


def ready():
	"""Switched on, with every setting a call needs."""
	return automation.is_enabled(SWITCH) and all(_config().values())


def form(doc):
	"""The dialog: project and issue type from the operator's projects, the record's title as the summary, a note."""
	projects = _projects()
	types = {project: _issue_types(project) for project in projects}
	return {
		"title": _(LABEL),
		"submit": _("Raise issue"),
		"fields": [
			{"fieldname": "project", "label": _("Project"), "fieldtype": "Select", "reqd": 1, "options": projects, "default": projects[0]},
			{"fieldname": "issue_type", "label": _("Issue type"), "fieldtype": "Select", "reqd": 1,
			 "options": types[projects[0]], "default": (types[projects[0]] or [None])[0], "options_by": {"project": types}},
			{"fieldname": "summary", "label": _("Summary"), "fieldtype": "Data", "reqd": 1, "default": _title(doc)},
			{"fieldname": "note", "label": _("What should engineering look at?"), "fieldtype": "Small Text"},
		],
	}


def run(doc, values):
	"""Raise the issue, then link the record to it inside Jira; returns `{key, url}`."""
	project, issue_type, summary = values.get("project"), values.get("issue_type"), (values.get("summary") or "").strip()
	if project not in _projects() or not issue_type or not summary:
		frappe.throw(_("Pick a project and an issue type, and write a summary."), title=_("Missing details"))
	key = _ask("POST", "/rest/api/3/issue", json={"fields": {
		"project": {"key": project},
		"issuetype": {"name": issue_type},
		"summary": summary[:255],
		"description": _description(doc, values.get("note")),
		"labels": _labels(doc),
	}})["key"]
	try:
		_link_back(key, doc)
	except JiraError:
		frappe.log_error(title="Record actions: Jira link back", message=f"{doc.doctype} {doc.name} -> {key}\n{frappe.get_traceback()}")
	return {"key": key, "url": f"{_config()['site']}/browse/{key}"}


def live(link):
	"""The issue as Jira holds it now; `error` when Jira cannot be read, so the card says so rather than failing."""
	try:
		issue = _call("GET", f"/rest/api/3/issue/{link.external_key}", params={"fields": LIVE_FIELDS})
	except JiraError as e:
		return {"error": str(e)}
	fields = issue.get("fields") or {}
	status = fields.get("status") or {}
	return {
		"title": fields.get("summary"),
		"status": status.get("name"),
		# Jira's status category: new, indeterminate or done.
		"stage": (status.get("statusCategory") or {}).get("key"),
		"owner": (fields.get("assignee") or {}).get("displayName"),
		"priority": (fields.get("priority") or {}).get("name"),
		"updated": fields.get("updated"),
	}


def on_ticket_closed(doc, method=None):
	"""HD Ticket on_update: a ticket that has just closed shows as resolved on every Jira issue raised from it."""
	if doc.get("status_category") not in subjects.TERMINAL_CATEGORIES or not doc.has_value_changed("status_category") or not ready():
		return
	from tatva_connect import record_actions

	if record_actions.links_of(doc.doctype, doc.name, KEY):
		frappe.enqueue(f"{__name__}.resolve_links", doctype=doc.doctype, name=doc.name, enqueue_after_commit=True)


def resolve_links(doctype, name):
	"""Mark the record's link resolved on each of its Jira issues; a refusal is logged, the next close tries again."""
	from tatva_connect import record_actions

	doc = frappe.get_doc(doctype, name)
	for link in record_actions.links_of(doctype, name, KEY):
		try:
			_link_back(link.external_key, doc, resolved=True)
		except JiraError:
			frappe.log_error(title="Record actions: Jira resolve", message=f"{doctype} {name} -> {link.external_key}\n{frappe.get_traceback()}")


def _link_back(key, doc, resolved=False):
	"""The record as a link in the issue's links panel; the same globalId updates it rather than adding a second."""
	body = {
		"globalId": f"tatva:{doc.doctype}:{doc.name}",
		"application": {"name": "TatvaCare"},
		"relationship": _("raised from"),
		"object": {"url": deep_link.app_url(doc.doctype, doc.name), "title": f"{_(DOCTYPES[doc.doctype])} {doc.name}", "summary": _title(doc)},
	}
	if resolved:
		body["object"]["status"] = {"resolved": True}
	_call("POST", f"/rest/api/3/issue/{key}/remotelink", json=body)


def _issue_types(project):
	"""The issue types a project creates, sub-tasks left out, by name."""
	page = _ask("GET", f"/rest/api/3/issue/createmeta/{project}/issuetypes", params={"maxResults": 50})
	return [t["name"] for t in (page.get("issueTypes") or page.get("createMetaIssueType") or []) if not t.get("subtask")]


def _description(doc, note):
	"""Atlassian Document Format: the person's note, then where the issue came from as a link."""
	url = deep_link.app_url(doc.doctype, doc.name)
	where = _("Raised from {0} {1} by {2}: ").format(_(DOCTYPES[doc.doctype]), doc.name, get_fullname(frappe.session.user))
	paragraphs = [[{"type": "text", "text": note.strip()}]] if (note or "").strip() else []
	paragraphs.append([{"type": "text", "text": where}, {"type": "text", "text": url, "marks": [{"type": "link", "attrs": {"href": url}}]}])
	return {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": p} for p in paragraphs]}


def _labels(doc):
	"""The record's grain as one Jira label, such as goodflip-india-inside-sales; Jira labels take no spaces."""
	axes = [a for a in grain.own_of(doc) if a]
	return [re.sub(r"[^a-z0-9]+", "-", "-".join(axes).lower()).strip("-")] if axes else []


def _title(doc):
	return doc.get(frappe.get_meta(doc.doctype).get_title_field()) or doc.name


def _projects():
	return [p.strip() for p in (frappe.get_cached_doc(SETTINGS).jira_projects or "").splitlines() if p.strip()]


def _config():
	doc = frappe.get_cached_doc(SETTINGS)
	return {"site": doc.jira_site, "email": doc.jira_email, "token": doc.get_password("jira_api_token", raise_exception=False),
	        "projects": doc.jira_projects}


def _ask(method, path, **kwargs):
	"""`_call` for a person waiting on the answer: Jira's refusal becomes their error message."""
	try:
		return _call(method, path, **kwargs)
	except JiraError as e:
		frappe.throw(str(e), title=_("Jira"))


def _call(method, path, json=None, params=None):
	"""One authenticated Jira call through frappe's own request helper; a refusal or silence raises JiraError with Jira's words."""
	config = _config()
	frappe.flags.integration_request = None  # a timeout sets no response, so the last call's must not be read
	try:
		return make_request(method, config["site"] + path, auth=(config["email"], config["token"]),
		                    headers={"Accept": "application/json"}, json=json, params=params, timeout=TIMEOUT) or {}
	except Exception as e:
		response = frappe.flags.integration_request
		if response is None:
			raise JiraError(_("Jira did not answer. Try again in a moment.")) from e
		raise JiraError(_("Jira refused: {0}").format(_reason(response))) from e


def _reason(response):
	"""Jira's own error words (`errorMessages`, `errors`), else its HTTP status."""
	try:
		body = response.json()
	except ValueError:
		return str(response.status_code)
	words = [*(body.get("errorMessages") or []), *(f"{k}: {v}" for k, v in (body.get("errors") or {}).items())]
	return "; ".join(words) or str(response.status_code)
