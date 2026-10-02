"""The CRM bell, read from frappe's Notification Log: the one store every notice lives in, scoped to the CRM's apps."""
import frappe
from crm.api.notifications import EVENT, _full_names
from frappe.utils import cint

LOG = "Notification Log"
# The apps whose notices are the CRM's; frappe stamps `app` from the record's doctype (Notification Log before_insert).
APPS = ("crm", "tatva_connect")
# The SPA route a notice opens, by the record it names; a task opens on its lead or deal.
ROUTES = {"CRM Lead": "Lead", "CRM Deal": "Deal", "CRM Smart View": "SmartViews"}
TASK = "CRM Task"


FIELDS = ["name", "creation", "type", "title", "subject", "from_user", "for_user", "read",
          "document_type", "document_name", "source_doctype", "source_name"]


def _scope(user, **more):
	return {"for_user": user, "app": ["in", APPS], **more}


def unread_count(user=None) -> int:
	return frappe.db.count(LOG, _scope(user or frappe.session.user, read=0))


def _task_parents(rows) -> dict:
	"""{task: (doctype, name)} for every task a page names, in one read."""
	tasks = {r.document_name for r in rows if r.document_type == TASK}
	if not tasks:
		return {}
	return {
		t.name: (t.reference_doctype, t.reference_docname)
		for t in frappe.get_all(TASK, filters={"name": ["in", list(tasks)]}, fields=["name", "reference_doctype", "reference_docname"])
	}


def _item(row, names, parents):
	doctype, name, hash_ = row.document_type, row.document_name, ""
	if doctype == TASK:
		(doctype, name), hash_ = parents.get(name, (None, None)), "#tasks"
	if row.type == "Mention" and row.source_name:
		hash_ = "#" + row.source_name
	if row.type == "WhatsApp":
		hash_ = "#whatsapp"
	return {
		"name": row.name,
		"creation": row.creation,
		"from_user": {"name": row.from_user, "full_name": names.get(row.from_user)},
		"type": row.type,
		"to_user": row.for_user,
		"read": row.read,
		"hash": hash_,
		"notification_text": f'<div class="mb-2 leading-5 text-ink-gray-5">{row.title or row.subject or ""}</div>',
		# The tray marks a row read by this key; a Notification Log row is its own key.
		"notification_type_doc": row.name,
		"reference_doctype": (doctype or "").replace("CRM ", "").lower(),
		"reference_name": name,
		"route_name": ROUTES.get(doctype),
	}


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


@frappe.whitelist()
def mark_as_read(user: str | None = None, doc: str | None = None):
	"""crm's mark_as_read, over Notification Log: the caller's own CRM notices, or the one named; `user` is ignored, never trusted."""
	user = frappe.session.user
	frappe.db.set_value(LOG, _scope(user, read=0, **({"name": doc} if doc else {})), "read", 1, update_modified=False)
	frappe.db.commit()  # nosemgrep: the read receipt is the whole request; the event below must not describe an uncommitted state
	frappe.publish_realtime(EVENT, {"unread": unread_count(user)}, user=user)
