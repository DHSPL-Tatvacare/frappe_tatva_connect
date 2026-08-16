# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Contact visibility — a colleague is not a customer.

Frappe creates one Contact per User, so every rep, manager and operator who has ever been given a login
is also a row in the Contact list. Those rows are STAFF, not customers, and unfiltered they crowd every
contact surface with the team's own names — a rep searching for a patient finds their own manager, and
picking one attaches a colleague to a deal.

WHO IS STAFF: a contact whose linked User is a **System User**. Never "has a user set". Website Users
hold logins too and are legitimate external people, so a naive `user is set` filter would hide real
customers — that is the defect this rule is shaped around, and it has a test of its own.

HIDE, NEVER DELETE. Frappe keeps creating a Contact per User and we do not fight the framework; we just
stop showing them here. Staff stay findable where staff belong: the User doctype.

SWITCHABLE, like every other permission gate. The four `::visibility` rows in the automation registry
each carry a switch an operator can read and reverse; this one was enforced always, so it carried none.
It ships dormant on the same terms: off is stock frappe, and arming it is the operator's call.

PRIVILEGE: a privileged caller sees everything, staff contacts included. Asked of the ONE spelling in
`visibility.is_privileged` — never a retyped role check.

ONE HOOK, THREE SURFACES. `frappe/model/db_query.py:1160` collects every app's
`permission_query_conditions` for a doctype and ANDs them, so this composes with frappe's own Contact
condition rather than replacing it. The CRM contact list, the Convert "Choose Existing" picker
(`<Link doctype="Contact">` -> `search_link` -> `search_widget`) and Helpdesk's `search_contacts`
(`frappe.get_list`) all resolve through that one gate, so no helpdesk code is touched.

FRAPPE'S OWN PLUMBING IS UNAFFECTED. `frappe.get_all` and `frappe.db.exists` do not apply permission
conditions, and `frappe.contacts.doctype.contact.contact.get_contact_name` uses exactly those two, so
user creation and update keep resolving a staff member's own contact as they do today.
"""
import frappe

from tatva_connect import automation
from tatva_connect.access import visibility

# The User.user_type value that means "one of us". Website User is the other, and it stays visible.
STAFF_USER_TYPE = "System User"

# The operator's own switch for this rule. Dormant means stock frappe: every contact, staff included.
SWITCH = "Contact::Contact::visibility"


def get_contact_permission_query_conditions(user=None):
	"""Hide staff contacts from every unprivileged caller. `None` means nothing to restrict."""
	if not automation.is_enabled(SWITCH) or visibility.is_privileged(user):
		return None
	# ONE correlated predicate the optimiser drives off `tabUser.name`, never a name list inlined from
	# Python — that clause would grow with headcount and be stale the moment a login is created.
	# NOT EXISTS, not NOT IN: a contact with no user compares against nothing and stays visible.
	return (
		"not exists (select 1 from `tabUser` where `tabUser`.`name` = `tabContact`.`user`"
		f" and `tabUser`.`user_type` = {frappe.db.escape(STAFF_USER_TYPE)})"
	)
