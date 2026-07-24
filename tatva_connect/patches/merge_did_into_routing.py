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

from tatva_connect import phone
from tatva_connect.telephony import envelope as env

OLD = "CRM Telephony DID"
ROUTING = "CRM Telephony Routing"
DID_CHILD = "CRM Telephony Routing DID"


def execute():
	if not frappe.db.exists("DocType", OLD):
		return

	skipped = []
	for row in frappe.get_all(
		OLD,
		fields=["name", "did_number", "label", "telephony_account", "enabled",
		        "vertical", "psp_group", "program"],
	):
		reason = _unusable(row)
		if reason:
			skipped.append(f"{row.name} ({row.did_number}): {reason}")
			continue
		digits = phone.match_digits(row.did_number, last=10)
		if frappe.db.exists(DID_CHILD, {"did_number": digits, "parenttype": ROUTING}):
			continue

		try:
			rule = _rule_for(row)
			if rule is None:
				skipped.append(f"{row.name} ({row.did_number}): no telephony account, and its grain has no existing rule to inherit one from")
				continue
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
			rule.save(ignore_permissions=True)  # authz-ok: tier-a — migration, runs as Administrator at migrate
			frappe.db.commit()
		except Exception:
			# One bad number must not abort the migrate. The rule controller re-validates EVERY existing DID on save, so a row that predates the current phone rule throws here on a number we never touched.
			frappe.db.rollback()
			skipped.append(f"{row.name} ({row.did_number}): {frappe.get_traceback(with_context=False)}")

	if skipped:
		# Named, not silently dropped: the operator adds each of these in the desk afterwards. Until then the number routes nothing — which is already what a DID with no rule did.
		frappe.log_error(
			title="telephony merge: DIDs not carried onto a routing rule",
			message="These numbers were NOT migrated and route nothing until an operator adds them:\n" + "\n".join(skipped),
		)

	# The doctype goes even with rows skipped: they are recorded above, and leaving the table would keep a
	# second, disagreeing map alive — the exact thing this patch exists to end.
	frappe.delete_doc("DocType", OLD, force=True, ignore_permissions=True)  # authz-ok: tier-a — migration, runs as Administrator at migrate
	frappe.db.commit()


def _unusable(row):
	"""Why this DID cannot become a routing rule's child, or "" if it can.

	Each of these reaches a `frappe.throw` in the routing controller, so it is decided HERE where it can be
	logged, not there where it aborts the whole migrate. No fallback to `row.name`: a doctype name is not a
	phone number, and passing one on would fail the controller's own digit check anyway.
	"""
	if not phone.match_digits(row.did_number, last=10):
		return f"not a full phone number (needs {env.PHONE_MIN_DIGITS} digits)"
	if not (row.vertical or row.psp_group or row.program):
		return "no grain — an all-blank rule is a global catch-all, which the routing controller forbids"
	return ""


def _rule_for(row):
	"""The rule for this DID's grain, created if the grain has none — or None when it cannot be created.

	Matched on the canonical triple in Python, the same comparison the routing controller makes, so a
	blank axis stored as NULL and one stored as "" resolve to the same rule.
	"""
	triple = (row.vertical or "", row.psp_group or "", row.program or "")
	for existing in frappe.get_all(ROUTING, fields=["name", "vertical", "psp_group", "program"]):
		if (existing.vertical or "", existing.psp_group or "", existing.program or "") == triple:
			return frappe.get_doc(ROUTING, existing.name)

	# telephony_account is reqd on the rule. An existing rule supplies it (the branch above), a new one cannot invent it — so the number is reported instead of aborting the migrate on a mandatory-field throw.
	if not row.telephony_account:
		return None

	rule = frappe.new_doc(ROUTING)
	rule.update({
		"vertical": row.vertical or None,
		"psp_group": row.psp_group or None,
		"program": row.program or None,
		"telephony_account": row.telephony_account,
	})
	rule.insert(ignore_permissions=True)  # authz-ok: tier-a — migration, runs as Administrator at migrate
	return rule
