"""Fold CRM Telephony DID into CRM Telephony Routing as a child table, and drop the old doctype.

The two tables described the same thing from opposite ends. Routing said grain -> account, for an
outbound call. The DID map said number -> grain + account, for an inbound one. Nothing made them agree,
and they did not: a grain carried four DIDs and no routing rule at all, so every call on those numbers
was attributed correctly on the way in and every pull for those leads resolved no account and did
nothing. One table cannot disagree with itself.

A number now lives inside the rule for the grain that owns it, so a mapped DID without a route is no
longer expressible.

Each DID is moved onto the rule for its grain, and the rule is CREATED when the grain has none — which
is the case the old split produced and the whole reason this runs. A DID whose account differs from its
rule's keeps that account as a per-number override, so a grain reached by two providers survives.
"""
import frappe

from tatva_connect.telephony import envelope as env

OLD = "CRM Telephony DID"
ROUTING = "CRM Telephony Routing"
DID_CHILD = "CRM Telephony Routing DID"


def execute():
	if not frappe.db.exists("DocType", OLD):
		return

	for row in frappe.get_all(
		OLD,
		fields=["name", "did_number", "label", "telephony_account", "enabled",
		        "vertical", "psp_group", "program"],
	):
		digits = env.phone_digits(row.did_number) or row.name
		if not digits:
			frappe.log_error(title="telephony merge: DID has no usable number", message=str(row))
			continue
		if frappe.db.exists(DID_CHILD, {"did_number": digits, "parenttype": ROUTING}):
			continue

		rule = _rule_for(row)
		rule.append("dids", {
			"did_number": digits,
			"label": row.label,
			"enabled": row.enabled,
			# Only a genuine divergence is carried over. Repeating the rule's own account on every
			# child row would turn a default into 30 copies of itself.
			"telephony_account": (
				row.telephony_account if row.telephony_account != rule.telephony_account else None
			),
		})
		rule.save(ignore_permissions=True)

	frappe.db.commit()

	# The table goes only once every row is on a rule. A failure above leaves the old doctype in place
	# and the patch re-runnable, rather than dropping numbers that never landed.
	frappe.delete_doc("DocType", OLD, force=True, ignore_permissions=True)
	frappe.db.commit()


def _rule_for(row):
	"""The rule for this DID's grain, created if the grain has none.

	Matched on the canonical triple in Python, the same comparison the routing controller makes, so a
	blank axis stored as NULL and one stored as "" resolve to the same rule.
	"""
	triple = (row.vertical or "", row.psp_group or "", row.program or "")
	for existing in frappe.get_all(ROUTING, fields=["name", "vertical", "psp_group", "program"]):
		if (existing.vertical or "", existing.psp_group or "", existing.program or "") == triple:
			return frappe.get_doc(ROUTING, existing.name)

	rule = frappe.new_doc(ROUTING)
	rule.update({
		"vertical": row.vertical or None,
		"psp_group": row.psp_group or None,
		"program": row.program or None,
		"telephony_account": row.telephony_account,
	})
	rule.insert(ignore_permissions=True)
	return rule
