"""The CRM bell, read from frappe's Notification Log: the one store every notice lives in, scoped to the CRM's apps."""
from urllib.parse import urlparse

import frappe
from crm.api.notifications import EVENT, _full_names
from frappe.utils import cint, strip_html_tags
from frappe.utils.html_utils import unescape_html

from tatva_connect.notifications import deep_link
from tatva_connect.notifications.types import WHATSAPP

LOG = "Notification Log"
# The apps whose notices are the CRM's; frappe stamps `app` from the record's doctype (Notification Log before_insert).
APPS = ("crm", "tatva_connect")
TASK = "CRM Task"
# The SPA's own base; a notice opening anywhere else (Desk, Helpdesk) has no route in the CRM bell.
SPA = "/crm"


FIELDS = ["name", "creation", "type", "title", "subject", "from_user", "for_user", "read",
          "document_type", "document_name", "source_doctype", "source_name", "link"]


def _scope(user, **more):
	return {"for_user": user, "app": ["in", APPS], **more}


def unread_count(user=None) -> int:
	return frappe.db.count(LOG, _scope(user or frappe.session.user, read=0))


def _task_parents(rows) -> dict:
	"""{task: (doctype, name)} for every task a page names, in one read."""
	tasks = {r.document_name for r in rows if r.document_type == TASK and r.document_name}
	if not tasks:
		return {}
	return {
		t.name: (t.reference_doctype, t.reference_docname)
		for t in frappe.get_all(TASK, filters={"name": ["in", list(tasks)]}, fields=["name", "reference_doctype", "reference_docname"])
	}


def _item(row, names, parents):
	doctype, name, hash_ = row.document_type, row.document_name, ""
	if doctype == TASK and name:
		(doctype, name), hash_ = parents.get(name, (None, None)), "#tasks"
	if row.type == "Mention" and row.source_name:
		hash_ = "#" + row.source_name
	if row.type == WHATSAPP:
		hash_ = "#whatsapp"
	return {
		"name": row.name,
		"creation": row.creation,
		"from_user": {"name": row.from_user, "full_name": names.get(row.from_user)},
		"type": row.type,
		"to_user": row.for_user,
		"read": row.read,
		# Plain text: frappe's subject carries markup, and the tray prints text, never HTML.
		"notification_text": unescape_html(strip_html_tags(row.title or row.subject or "")),
		# The tray marks a row read by this key; a Notification Log row is its own key.
		"notification_type_doc": row.name,
		"reference_doctype": (doctype or "").replace("CRM ", "").lower(),
		"reference_name": name,
		"route": _route(doctype, name, row.link, hash_),
	}


def _route(doctype, name, link, hash_):
	"""The SPA path a notice opens, off deep_link's one map: the record it names, else the list its link drills to, as email and push open."""
	if name:
		path = deep_link.app_path(doctype, name)
	elif link:
		url = urlparse(link)
		path = url.path + (f"?{url.query}" if url.query else "")
	else:
		path = deep_link.LISTS.get(doctype)
	return path[len(SPA):] + hash_ if path and path.startswith(SPA + "/") else None


@frappe.whitelist()
def get_notifications(limit: int = 50, start: int = 0):
	"""crm's get_notifications, over Notification Log: one page newest first, the unread total, and whether more exist."""
	limit, start, user = cint(limit) or 50, cint(start), frappe.session.user
	# One row past the page tells whether more exist, without a second COUNT.
	rows = frappe.get_all(LOG, filters=_scope(user), fields=FIELDS, order_by="creation desc",  # authz-ok: tier-a — the caller's own notices, scoped by for_user
	                      limit_start=start, limit=limit + 1)
	has_more = len(rows) > limit
	rows = rows[:limit]
	names, parents = _full_names(rows), _task_parents(rows)
	return {"items": [_item(r, names, parents) for r in rows], "unread": unread_count(user), "has_more": has_more}


@frappe.whitelist(methods=["POST"])
def mark_as_read(user: str | None = None, doc: str | None = None):
	"""crm's mark_as_read, over Notification Log: the caller's own CRM notices, or the one named; `user` is ignored, never trusted."""
	user = frappe.session.user
	frappe.db.set_value(LOG, _scope(user, read=0, **({"name": doc} if doc else {})), "read", 1, update_modified=False)
	frappe.db.commit()  # nosemgrep: the read receipt is the whole request; the event below must not describe an uncommitted state
	frappe.publish_realtime(EVENT, {"unread": unread_count(user)}, user=user)
