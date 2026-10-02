# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Platform rule, one contract for every app's invitation document: it reaches only an address on a `Tatva Platform Settings` domain; blank is off (A.6)."""
import re

import frappe
from crm.fcrm.doctype.crm_invitation.crm_invitation import CRMInvitation
from frappe import _
from frappe.core.doctype.user_invitation.user_invitation import UserInvitation
from frappe.utils import cstr

SETTINGS = "Tatva Platform Settings"
# A host name: labels of letters, digits and hyphens joined by dots, ending in a letters-only label.
_DOMAIN = re.compile(r"^(?!-)[a-z0-9-]+(\.[a-z0-9-]+)*\.[a-z]{2,}$")


def domains_of(text):
	"""The domains in a one-per-line list, lowercased and de-duplicated, a leading `@` dropped; an entry that is not a domain is refused."""
	domains = list(dict.fromkeys(line.strip().lower().lstrip("@") for line in cstr(text).splitlines() if line.strip()))
	bad = [d for d in domains if not _DOMAIN.match(d)]
	if bad:
		frappe.throw(_("Not a domain: {0}. Write one per line, like tatvacare.in.").format(", ".join(bad)))
	return domains


def assert_allowed(email):
	"""Refuse an invitation to an address outside the allowed domains; a blank list allows any address."""
	allowed = domains_of(frappe.db.get_single_value(SETTINGS, "invitation_domains"))
	if allowed and cstr(email).strip().lower().rpartition("@")[2] not in allowed:
		frappe.throw(
			_("Invitations can only be sent to addresses on: {0}").format(", ".join(allowed)),
			title=_("Invitation not sent"),
		)


class TatvaUserInvitation(UserInvitation):
	def before_insert(self):
		assert_allowed(self.email)  # before super(), so a refused invitation is never marked pending or mailed
		super().before_insert()


class TatvaCRMInvitation(CRMInvitation):
	def before_insert(self):
		assert_allowed(self.email)  # before super(), so a refused invitation mints no key and sends no mail
		super().before_insert()
