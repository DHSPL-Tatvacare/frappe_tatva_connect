# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Platform rule, one contract for every app's invitation document: it reaches only an allowed `Tatva Platform Settings` domain and never a blocked one; blank lists are off (A.6)."""
import re

import frappe
from crm.fcrm.doctype.crm_invitation.crm_invitation import CRMInvitation
from frappe import _
from frappe.core.doctype.user_invitation.user_invitation import UserInvitation
from frappe.utils import cstr

SETTINGS = "Tatva Platform Settings"
# A host name: labels of letters, digits and hyphens joined by dots, ending in a letters-only label.
_DOMAIN = re.compile(r"^(?!-)[a-z0-9-]+(\.[a-z0-9-]+)*\.[a-z]{2,}$")


FIELD = "invitation_domain_rules"


def clean_domain(value):
	"""One domain, lowercased with a leading `@` dropped; anything that is not a domain is refused."""
	domain = cstr(value).strip().lower().lstrip("@")
	if not _DOMAIN.match(domain):
		frappe.throw(_("Not a domain: {0}. Write it like tatvacare.in.").format(value))
	return domain


def split(rows):
	"""`(allowed, blocked)` from Invitation Domains rows — the one reading the save message and the gate share."""
	return [r.domain for r in rows if r.rule == "Allowed"], [r.domain for r in rows if r.rule == "Blocked"]


def rules():
	"""The saved grid, as validate cleaned it."""
	return split(frappe.get_cached_doc(SETTINGS).get(FIELD) or [])


def assert_allowed(email):
	"""Refuse an invitation to an address outside the allowed domains or on a blocked one; no Allowed row allows any address."""
	allowed, blocked = rules()
	domain = cstr(email).strip().lower().rpartition("@")[2]
	if allowed and domain not in allowed:
		frappe.throw(_("{0} is not an allowed invitation domain.").format(domain), title=_("Invitation not sent"))
	if domain in blocked:
		frappe.throw(_("{0} is a blocked invitation domain.").format(domain), title=_("Invitation not sent"))


class TatvaUserInvitation(UserInvitation):
	def before_insert(self):
		assert_allowed(self.email)  # before super(), so a refused invitation is never marked pending or mailed
		super().before_insert()


class TatvaCRMInvitation(CRMInvitation):
	def before_insert(self):
		assert_allowed(self.email)  # before super(), so a refused invitation mints no key and sends no mail
		super().before_insert()
