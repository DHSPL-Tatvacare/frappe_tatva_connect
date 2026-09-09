# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Who a reader sees behind a change — the person, or the channel that stood in for one.

Every change is saved by a LOGIN, and the login already says which channel made it: a visitor filling an
enrolment form is the literal `Guest`, a partner's API key is one User named by its contract, a migration
or a patch is `Administrator`, and everything else is a rep. Nothing new is stored to answer this.

ONE rule (`_kind`), two readers: `resolve` for a page of rows, `label` for a row that stands alone.
"""

import frappe
from frappe import _
from frappe.utils.caching import request_cache

# Intake is the only path that writes a lead as an anonymous visitor; every other guest endpoint here reads.
GUEST = "Guest"
SYSTEM_USERS = frozenset({"Administrator"})

@request_cache
def partner_users() -> dict:
	"""`{user: source}` for every enabled partner contract — fifteen rows, read once per request.

	`request_cache` and not a redis key: a cached list needs invalidating and `get_cached_doc` cannot
	stand in, because a mapping is named by its grain and contract, never by the user it authorises."""
	return {
		m["partner_user"]: m.get("source")
		for m in frappe.get_all(
			"CRM Lead API Mapping", filters={"enabled": 1}, fields=["partner_user", "source"]
		)
		if m.get("partner_user")
	}


def _kind(user, partners) -> str:
	"""The ONE rule. `kind` is what the reader is looking at, never a role."""
	if user == GUEST:
		return "intake"
	if user in partners:
		return "api"
	if user in SYSTEM_USERS:
		return "system"
	return "person"


def _channel_label(user, kind, partners) -> str:
	"""What a non-person channel is called. A person is named by their User row, not from here."""
	if kind == "intake":
		return _("Intake form")
	if kind == "api":
		return _("{0} · via API").format(partners.get(user) or _("Partner"))
	return _("System")


def resolve(users) -> dict:
	"""`{user: {"label", "kind"}}` for many logins in at most two reads — never one per row."""
	wanted = {u for u in users if u}
	if not wanted:
		return {}

	partners = partner_users()
	kinds = {u: _kind(u, partners) for u in wanted}
	people = [u for u, k in kinds.items() if k == "person"]

	# Only the logins that turned out to be people cost a User read, and they cost ONE between them.
	names = (
		{
			u["name"]: u["full_name"]
			for u in frappe.get_all("User", filters={"name": ["in", sorted(people)]}, fields=["name", "full_name"])
		}
		if people
		else {}
	)
	return {
		user: {
			"label": (names.get(user) or user) if kind == "person" else _channel_label(user, kind, partners),
			"kind": kind,
		}
		for user, kind in kinds.items()
	}


def label(user) -> str:
	"""One login. Reads the User through frappe's document cache, so repeat calls in a request are free."""
	if not user:
		return user
	partners = partner_users()
	kind = _kind(user, partners)
	if kind != "person":
		return _channel_label(user, kind, partners)
	return frappe.get_cached_value("User", user, "full_name") or user
