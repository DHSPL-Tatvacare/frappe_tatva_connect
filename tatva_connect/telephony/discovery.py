"""What Acefone already knows, offered to the operator: which number reaches which department, and which
extension belongs to whom.

Both answers speak one shape — `{rows, refused}`, each row carrying `group`, `taken` and `taken_by` —
so the two Desk dialogs are the same dialog with different columns. Neither writes: the operator ticks,
the rows land on the form, and the save is an ordinary one."""
import frappe
from frappe import _

from tatva_connect import phone
from tatva_connect.telephony import api, resolve
from tatva_connect.telephony.adapters import acefone


@frappe.whitelist()
def numbers_for_rule(routing: str) -> dict:
	"""This account's numbers, grouped by the department each reaches; one the rule or another grain holds is taken."""
	frappe.has_permission(resolve.ROUTING_DOCTYPE, "write", routing, throw=True)
	rule = frappe.get_doc(resolve.ROUTING_DOCTYPE, routing)
	if not rule.telephony_account:
		return _answer([], [{"account": "", "reason": _("Set this rule's account first.")}])

	account = frappe.get_doc(acefone.ACCOUNT_DT, rule.telephony_account)
	# A number belongs to exactly one grain, so one another rule lists is shown as theirs and never offered.
	owner = {
		row.did_number: row.parent
		for row in frappe.get_all(
			resolve.DID_CHILD,
			filters={"parenttype": resolve.ROUTING_DOCTYPE},
			fields=["did_number", "parent"],
		)
	}

	answer = api.get_my_numbers(account)
	if _refusal(answer):
		return _answer([], [{"account": account.name, "reason": _refusal(answer)}])

	rows = []
	for number in _rows(answer):
		digits = phone.match_digits(number.get("alias") or number.get("did"), last=10)
		if not digits:
			continue
		rows.append({
			"group": number.get("destination_name") or "",
			"taken": digits in owner,
			"taken_by": owner.get(digits),
			"name": digits,
		})
	return _answer(rows, [], account=account.name)


@frappe.whitelist()
def extensions_for_agent(agent: str) -> dict:
	"""Every extension on every enabled account, grouped by account; one any rep holds is taken."""
	frappe.has_permission(resolve.AGENT_DOCTYPE, "write", agent, throw=True)

	rows, refused = [], []
	for name in frappe.get_all(
		acefone.ACCOUNT_DT, filters={"provider": acefone.PROVIDER, "enabled": 1}, pluck="name"
	):
		account = frappe.get_doc(acefone.ACCOUNT_DT, name)
		answer = api.get_users(account)
		if _refusal(answer):
			# One account's credentials are not the others': the dialog names it and still lists the rest.
			refused.append({"account": name, "reason": _refusal(answer)})
			continue

		owner = resolve.seats_for_account(name)["by_seat"]
		departments = _departments_by_agent(account)
		for user in _rows(answer):
			extension = (user.get("extension") or "").strip()
			if not extension:
				continue
			rows.append({
				"group": name,
				"taken": extension in owner,
				"taken_by": owner.get(extension),
				"name": extension,
				"agent_name": (user.get("agent") or {}).get("name") or user.get("name") or "",
				"departments": ", ".join(departments.get((user.get("agent") or {}).get("id"), [])),
			})
	return _answer(rows, refused)


def _answer(rows, refused, account=None) -> dict:
	"""The one shape both dialogs read: every row `{group, name, taken, taken_by}`, and what the provider would not answer."""
	return {
		"account": account,
		"rows": sorted(rows, key=lambda r: (r["taken"], r["group"] or "~", r["name"])),
		"refused": refused,
	}


def _departments_by_agent(account) -> dict:
	"""Acefone agent id -> the departments it sits in, on this account."""
	found = {}
	for department in _rows(api.get_departments(account)):
		for member in department.get("agents") or []:
			found.setdefault(member.get("eid"), []).append(department.get("name") or "")
	return found


def _rows(response) -> list:
	"""The list inside an Acefone answer, empty when it refused — `_refusal` is what names the reason."""
	if isinstance(response, list):
		return response
	if isinstance(response, dict) and isinstance(response.get("data"), list):
		return response["data"]
	return []


def _refusal(response):
	"""How Acefone worded its refusal, or None when it answered."""
	if isinstance(response, dict) and response.get("status_code") is not None:
		return str(response.get("message") or response.get("status_code"))
	return None
