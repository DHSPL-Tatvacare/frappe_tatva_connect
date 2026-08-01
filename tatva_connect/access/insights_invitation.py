# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""An Insights invitation may only reach someone who already signs in here.

Upstream's flow creates a NEW `User` for any address, appends the `Insights User` role and logs the
visitor straight in from a link (`accept_invitation` is allow_guest) — so an Insights Admin could mint
a working login on this CRM for any mailbox on earth, and Website Settings' `disable_signup` never
sees it because nothing goes through sign-up.

The gate sits on the DOCUMENT, not on `insights.api.user.invite_users`. An Insights Admin also holds
`create` on `Insights User Invitation`, so the API method is one door of several and `before_insert` is
the only choke point on all of them (button, generic insert, desk form). A `doc_events` hook would need
a `CRM Tatva Automation` registry row (`automation/drift.assert_registered`) and a security floor is not
an operator toggle — `override_doctype_class` is explicitly excluded from that check and is the
sanctioned seam for changing another app's backend from here.

What still works: inviting a colleague, which is the only legitimate use — they already have a login,
so `create_user_if_not_exists` finds them and nothing is minted.
"""
import frappe
from frappe import _
from frappe.utils import cstr
from insights.insights.doctype.insights_user_invitation.insights_user_invitation import (
	InsightsUserInvitation,
)


class TatvaInsightsUserInvitation(InsightsUserInvitation):
	def before_insert(self):
		self.email = cstr(self.email).strip()  # accept() keys create_user_if_not_exists off this exact string
		assert_invitee_signs_in_here(self.email)  # before super(), so a refusal mints no key and sends no mail
		super().before_insert()


def assert_invitee_signs_in_here(email):
	"""Refuse unless `email` is a live login here. `User.name` IS the address and the column collates
	case-insensitively, so this is one primary-key read that also answers "is it enabled": None = no
	such account, 0 = disabled, 1 = fine. Permission-bypassing on purpose — it validates rather than
	returns, and the only caller who reaches it can already list every user."""
	email = cstr(email).strip()
	enabled = frappe.db.get_value("User", email, "enabled")

	if enabled is None:
		frappe.throw(
			_(
				"{0} has no login on this site. Insights access is granted to people who already sign in "
				"here — create the account first, or invite a colleague."
			).format(frappe.bold(email)),
			title=_("Invitation not sent"),
		)

	if not enabled:
		frappe.throw(
			_("{0} is disabled on this site. Re-enable the account before granting Insights access.").format(
				frappe.bold(email)
			),
			title=_("Invitation not sent"),
		)
