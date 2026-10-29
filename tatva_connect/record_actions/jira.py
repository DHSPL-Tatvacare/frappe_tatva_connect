# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Create a Jira issue from a ticket or a lead (Jira Cloud REST v3): a form built from what the chosen project's create
screen accepts, cached lookups, the issue, the record as a link inside Jira, and the issue's live status for the sidebar.
No patient detail is sent: the summary and description are the person's, and the record travels as its link."""
import re
from datetime import datetime

import frappe
from frappe import _
from frappe.integrations.utils import make_request
from frappe.utils import cint, convert_utc_to_system_timezone, get_datetime_str, get_fullname, getdate
from frappe.utils.caching import redis_cache

from tatva_connect import automation
from tatva_connect.automation import subjects
from tatva_connect.notifications import deep_link
from tatva_connect.taxonomy import grain

KEY = "jira_issue"
LABEL = "Create Jira issue"
LABEL_MADE = "Jira issue"
SECTION = "Engineering Details"
ICON = "/assets/tatva_connect/images/jira.svg"
# What a person calls each record this action serves, and the Atlassian Settings tick that offers it there.
DOCTYPES = {"HD Ticket": "Ticket", "CRM Lead": "Lead"}
OFFERED_BY = {"HD Ticket": "jira_on_tickets", "CRM Lead": "jira_on_leads"}
SETTINGS = "Atlassian Settings"
SWITCH = "Jira::Issues::raise"
# Seconds a Jira call may take before the person is told it did not answer.
TIMEOUT = 20
# How long Jira's lists are remembered: projects, create screens and labels change rarely; people and issues more often.
LISTS_TTL = 12 * 60 * 60
SEARCH_TTL = 10 * 60
LIVE_TTL = 2 * 60
# The issue fields the sidebar card reads.
LIVE_FIELDS = "summary,status,assignee,priority,updated"
# The create-screen fields the form shows beyond project, issue type, summary and description, in this order.
FORM_FIELDS = ("priority", "duedate", "assignee", "parent", "labels", "components")
# Fields Jira fills itself, or that this form sets on every issue.
SET_BY_FORM = {"project", "issuetype", "summary", "description", "reporter", *FORM_FIELDS}
PROJECT_KEY = re.compile(r"^[A-Z][A-Z0-9_]+$")


class JiraError(Exception):
	"""Jira refused or did not answer; the message is the person's to read."""


def ready(doctype=None):
	"""Switched on and set up, and offered on `doctype` when one is named."""
	if not (automation.is_enabled(SWITCH) and all(_config().values())):
		return False
	return not doctype or bool(frappe.get_cached_doc(SETTINGS).get(OFFERED_BY[doctype]))


def form(doc, values):
	"""The dialog for the project and issue type chosen so far (the first of each when none): the create screen's fields."""
	project, issue_type, projects = _choice(values)
	meta = _create_fields(project["key"], issue_type["id"])
	fields = [
		{"fieldname": "project", "label": _("Project"), "fieldtype": "Select", "reqd": 1, "reloads": 1, "half": 1, "default": project["key"],
		 "options": [{"label": f"{p['name']} ({p['key']})", "value": p["key"], "image": p["avatar"]} for p in projects]},
		{"fieldname": "issue_type", "label": _("Issue type"), "fieldtype": "Select", "reqd": 1, "reloads": 1, "half": 1, "default": issue_type["id"],
		 "options": [{"label": t["name"], "value": t["id"], "image": t["icon"]} for t in project["types"]]},
		{"fieldname": "summary", "label": _("Summary"), "fieldtype": "Data", "reqd": 1, "default": _title(doc)},
		{"fieldname": "description", "label": _("Description"), "fieldtype": "Small Text"},
	]
	fields += [_field(name, meta[name]) for name in FORM_FIELDS if name in meta]
	for field in fields:
		if field["fieldname"] == "labels" and _grain_label(doc):
			field["default"] = [_grain_label(doc)]
	unmet = [f["name"] for key, f in meta.items() if f["required"] and not f["has_default"] and key not in SET_BY_FORM]
	return {
		"title": _(LABEL),
		"submit": _("Create issue"),
		"icon": ICON,
		"fields": fields,
		"warning": _("Jira also needs {0} for this issue type; pick another type or fill it in Jira.").format(", ".join(unmet)) if unmet else None,
	}


def search(doc, field, query, values):
	"""People who can take an issue in the chosen project, or the project's issues to file under."""
	project, _type, _all = _choice(values)
	if field == "assignee":
		return _people(project["key"], query)
	if field == "parent":
		return _parents(project["key"], query)
	frappe.throw(_("{0} is not searched.").format(field))


def refresh():
	"""Forget every list read from Jira; the next form and search read Jira afresh."""
	for reader in (_projects, _create_fields, _people, _parents, _labels, _issue):
		reader.clear_cache()


def run(doc, values):
	"""Create the issue from what the chosen create screen accepts, then link the record to it inside Jira; returns `{key, url}`."""
	project, issue_type, _all = _choice(values)
	summary = (values.get("summary") or "").strip()
	if not summary:
		frappe.throw(_("Write a summary."), title=_("Missing details"))
	meta = _create_fields(project["key"], issue_type["id"])
	fields = {
		"project": {"key": project["key"]},
		"issuetype": {"id": issue_type["id"]},
		"summary": summary[:255],
		"description": _description(doc, values.get("description")),
	}
	given = {
		"priority": values.get("priority") and {"id": values["priority"]},
		"duedate": values.get("duedate") and str(getdate(values["duedate"])),
		"assignee": values.get("assignee") and {"accountId": values["assignee"]},
		"parent": values.get("parent") and {"key": values["parent"]},
		"labels": [re.sub(r"\s+", "-", label.strip()) for label in values.get("labels") or [] if label.strip()],
		"components": [{"id": c} for c in values.get("components") or []],
	}
	fields.update({name: value for name, value in given.items() if value and name in meta})
	key = _ask("POST", "/rest/api/3/issue", json={"fields": fields})["key"]
	try:
		_link_back(key, doc)
	except JiraError:
		frappe.log_error(title="Record actions: Jira link back", message=f"{doc.doctype} {doc.name} -> {key}\n{frappe.get_traceback()}")
	return {"key": key, "url": f"{_config()['site']}/browse/{key}"}


def live(link, fresh=False):
	"""The issue as Jira holds it (2 min cache, skipped when `fresh`); `error` when Jira cannot be read, so the card says so."""
	if fresh:
		_issue.clear_cache()
	try:
		return _issue(link.external_key)
	except JiraError as e:
		return {"error": str(e)}


def on_update(doc):
	"""A ticket that has just closed shows as resolved on every Jira issue created from it."""
	if doc.get("status_category") not in subjects.TERMINAL_CATEGORIES or not doc.has_value_changed("status_category"):
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


def whoami():
	"""The saved account's name as Atlassian knows it, for Check Connection."""
	if not all(_config().values()):
		frappe.throw(_("Fill in the site, account email and API token, and save, first."), title=_("Not set up"))
	return _ask("GET", "/rest/api/3/myself").get("displayName")


def _choice(values):
	"""The chosen project and issue type, or the first of each, with every project offered."""
	projects = _projects()
	if not projects:
		frappe.throw(_("The Jira account cannot create issues in any project."), title=_("Jira"))
	project = next((p for p in projects if p["key"] == values.get("project")), projects[0])
	if not project["types"]:
		frappe.throw(_("Jira has no issue types to create in {0}.").format(project["name"]), title=_("Jira"))
	issue_type = next((t for t in project["types"] if t["id"] == values.get("issue_type")), project["types"][0])
	return project, issue_type, projects


def _field(name, meta):
	"""One create-screen field as a form field, with Jira's own values where it lists them."""
	options = [{"label": v.get("name"), "value": v.get("id"), "image": v.get("iconUrl")} for v in meta["allowed"]]
	return {
		"priority": {"fieldname": name, "label": _("Priority"), "fieldtype": "Select", "options": options, "default": meta["default"], "half": 1},
		"duedate": {"fieldname": name, "label": _("Due date"), "fieldtype": "Date", "half": 1},
		"assignee": {"fieldname": name, "label": _("Assignee"), "fieldtype": "Search", "placeholder": _("Unassigned"), "half": 1},
		"parent": {"fieldname": name, "label": _("Parent"), "fieldtype": "Search", "placeholder": _("None"), "half": 1},
		"labels": {"fieldname": name, "label": _("Labels"), "fieldtype": "Tags", "allow_new": 1, "half": 1,
		           "options": [{"label": label, "value": label} for label in _labels()]},
		"components": {"fieldname": name, "label": _("Components"), "fieldtype": "Tags", "options": options, "half": 1},
	}[name] | {"reqd": cint(meta["required"] and not meta["has_default"])}


@redis_cache(ttl=LISTS_TTL)
def _projects():
	"""The live projects the account may create issues in, by name, each with its issue types; Jira pages them 100 at a time."""
	projects, start = [], 0
	while True:
		page = _ask("GET", "/rest/api/3/project/search", params={
			"action": "create", "status": "live", "expand": "issueTypes", "orderBy": "name", "startAt": start, "maxResults": 100})
		values = page.get("values") or []
		projects += [{
			"key": p["key"], "name": p["name"], "avatar": (p.get("avatarUrls") or {}).get("24x24"),
			"types": [{"id": t["id"], "name": t["name"], "icon": t.get("iconUrl")} for t in p.get("issueTypes") or [] if not t.get("subtask")],
		} for p in values]
		if page.get("isLast", True) or not values:
			return projects
		start += len(values)


@redis_cache(ttl=LISTS_TTL)
def _create_fields(project, type_id):
	"""What the project's create screen accepts for one issue type: each field, whether it is required, and its values."""
	fields, start = {}, 0
	while True:
		page = _ask("GET", f"/rest/api/3/issue/createmeta/{project}/issuetypes/{type_id}", params={"startAt": start, "maxResults": 100})
		rows = page.get("fields") or page.get("results") or []
		for f in rows:
			fields[f["fieldId"]] = {"name": f["name"], "required": bool(f.get("required")), "has_default": bool(f.get("hasDefaultValue")),
			                        "allowed": f.get("allowedValues") or [], "default": (f.get("defaultValue") or {}).get("id")}
		start += len(rows)
		if not rows or start >= (page.get("total") or 0):
			return fields


@redis_cache(ttl=SEARCH_TTL)
def _people(project, query):
	"""People who can be assigned issues in the project, matching `query` (everyone first when blank)."""
	users = _ask("GET", "/rest/api/3/user/assignable/search", params={"project": project, "query": query, "maxResults": 20})
	return [{"label": u.get("displayName"), "value": u["accountId"], "image": (u.get("avatarUrls") or {}).get("24x24"), "round": 1}
	        for u in users if u.get("accountType") == "atlassian" and u.get("active", True)]


@redis_cache(ttl=SEARCH_TTL)
def _parents(project, query):
	"""The project's issues matching `query`, to file the new one under; Jira refuses a parent its hierarchy does not allow."""
	if not PROJECT_KEY.match(project):
		return []
	page = _ask("GET", "/rest/api/3/issue/picker", params={"query": query, "currentJQL": f"project = {project}", "showSubTasks": "false"})
	site = _config()["site"]
	return [{"label": f"{i['key']} · {i.get('summaryText') or ''}", "value": i["key"], "image": site + i["img"] if i.get("img", "").startswith("/") else i.get("img")}
	        for section in page.get("sections") or [] if section.get("id") == "cs" for i in section.get("issues") or []]


@redis_cache(ttl=LISTS_TTL)
def _labels():
	"""Labels already used in Jira, to pick from; a person may still type a new one."""
	return _ask("GET", "/rest/api/3/label", params={"maxResults": 1000}).get("values") or []


@redis_cache(ttl=LIVE_TTL)
def _issue(key):
	issue = _call("GET", f"/rest/api/3/issue/{key}", params={"fields": LIVE_FIELDS})
	fields = issue.get("fields") or {}
	status = fields.get("status") or {}
	return {
		"title": fields.get("summary"),
		"status": status.get("name"),
		# Jira's status category: new, indeterminate or done.
		"stage": (status.get("statusCategory") or {}).get("key"),
		"owner": (fields.get("assignee") or {}).get("displayName"),
		"priority": (fields.get("priority") or {}).get("name"),
		"updated": _system_time(fields.get("updated")),
	}


def _system_time(stamp):
	"""Jira's offset time as the site's own `yyyy-mm-dd hh:mm:ss`, the shape every app reads a Frappe time in."""
	return get_datetime_str(convert_utc_to_system_timezone(datetime.fromisoformat(stamp))) if stamp else None


def _link_back(key, doc, resolved=False):
	"""The record as a link in the issue's links panel; the same globalId updates it rather than adding a second."""
	body = {
		"globalId": f"tatva:{doc.doctype}:{doc.name}",
		"application": {"name": "TatvaCare"},
		"relationship": _("created from"),
		"object": {"url": deep_link.app_url(doc.doctype, doc.name), "title": f"{_(DOCTYPES[doc.doctype])} {doc.name}", "summary": _title(doc)},
	}
	if resolved:
		body["object"]["status"] = {"resolved": True}
	_call("POST", f"/rest/api/3/issue/{key}/remotelink", json=body)


def _description(doc, note):
	"""Atlassian Document Format: the person's description, then where the issue came from as a link."""
	url = deep_link.app_url(doc.doctype, doc.name)
	where = _("Created from {0} {1} by {2}: ").format(_(DOCTYPES[doc.doctype]), doc.name, get_fullname(frappe.session.user))
	paragraphs = [[{"type": "text", "text": line}] for line in (note or "").strip().splitlines() if line.strip()]
	paragraphs.append([{"type": "text", "text": where}, {"type": "text", "text": url, "marks": [{"type": "link", "attrs": {"href": url}}]}])
	return {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": p} for p in paragraphs]}


def _grain_label(doc):
	"""The record's grain as one Jira label, such as goodflip-india-inside-sales; Jira labels take no spaces."""
	axes = [a for a in grain.own_of(doc) if a]
	return re.sub(r"[^a-z0-9]+", "-", "-".join(axes).lower()).strip("-") if axes else None


def _title(doc):
	return doc.get(frappe.get_meta(doc.doctype).get_title_field()) or doc.name


def _config():
	doc = frappe.get_cached_doc(SETTINGS)
	return {"site": doc.site, "email": doc.email, "token": doc.get_password("api_token", raise_exception=False)}


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
