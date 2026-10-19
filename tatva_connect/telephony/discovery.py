"""What the provider already knows, offered to the operator: which number reaches which department, and which
extension belongs to whom.

Both answers speak one shape — `{account, rows, refused}`, each row carrying `group`, `taken` and `taken_by` —
so the two Desk dialogs are one dialog with different columns. Neither writes: the operator picks, the rows
land on the form, and the form saves itself. Called only from a Fetch the operator pressed, on one account."""
import frappe
from frappe import _
from frappe.rate_limiter import rate_limit

from tatva_connect import phone
from tatva_connect.telephony import providers, resolve
from tatva_connect.utils import CheckReport


@frappe.whitelist(methods=["POST"])
def accounts_for(doctype: str, name: str) -> list:
	"""The enabled accounts a fetch on this rep or rule may ask through, for anyone who may edit it; names only."""
	if doctype not in (resolve.AGENT_DOCTYPE, resolve.ROUTING_DOCTYPE):
		frappe.throw(_("{0} fetches nothing through a telephony account.").format(doctype))
	frappe.has_permission(doctype, "write", name, throw=True)
	return _enabled_accounts()


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=10, seconds=60)
def numbers_for_rule(routing: str, account: str) -> dict:
	"""One account's numbers, grouped by what each rings; one this rule or another grain holds now is taken."""
	frappe.has_permission(resolve.ROUTING_DOCTYPE, "write", routing, throw=True)
	doc = _fetch_account(account)
	# A number belongs to one grain at a time, so one a rule holds now is shown as theirs and never offered.
	now = resolve.moment()
	owner = {
		row.did_number: row.parent
		for row in frappe.get_all(
			resolve.DID_CHILD,
			filters={"parenttype": resolve.ROUTING_DOCTYPE},
			fields=["did_number", "parent", "valid_from", "valid_to"],
		)
		if resolve.held_at(row, now)
	}

	answer = providers.adapter_for(doc).get_my_numbers(doc)
	refused = _refusal(answer)
	if refused:
		return _answer([], [{"account": account, "reason": refused}], account=account)

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
	return _answer(rows, [], account=account)


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=10, seconds=60)
def extensions_for_agent(agent: str, account: str) -> dict:
	"""One account's extensions, grouped by department; one another rep holds on its tenant is taken, this rep's own is not."""
	frappe.has_permission(resolve.AGENT_DOCTYPE, "write", agent, throw=True)
	doc = _fetch_account(account)
	adapter = providers.adapter_for(doc)
	answer = adapter.get_users(doc)
	refused = _refusal(answer)
	if refused:
		return _answer([], [{"account": account, "reason": refused}], account=account)

	# One extension is one person on the whole tenant, the rule the rep form saves under.
	owner = resolve.holdings_on_tenant(account)
	departments = _departments_by_agent(adapter, doc)
	rows = []
	for user in _rows(answer):
		extension = (user.get("extension") or "").strip()
		if not extension:
			continue
		rows.append({
			"group": ", ".join(departments.get((user.get("agent") or {}).get("id"), [])),
			"taken": owner.get(extension) not in (None, agent),
			"taken_by": owner.get(extension),
			"name": extension,
			"agent_name": (user.get("agent") or {}).get("name") or user.get("name") or "",
			"follow_me_number": (user.get("agent") or {}).get("follow_me_number") or "",
			"agent_status": adapter.agent_status(user),
		})
	return _answer(rows, [], account=account)


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=10, seconds=60)
def validate_token(doctype: str, name: str) -> dict:
	"""Whether this account's API token is accepted by its provider, and which accounts are the same provider login."""
	if doctype != resolve.ACCOUNT_DOCTYPE:
		frappe.throw(_("{0} carries no telephony token.").format(doctype))
	frappe.has_permission(doctype, "write", doc=name, throw=True)
	doc = frappe.get_doc(doctype, name)
	report = CheckReport()

	if not report.check(_("API token"), bool(doc.get_password("api_token", raise_exception=False)), _("not set")):
		return report.as_dict()
	answer = providers.adapter_for(doc).get_users(doc)
	refused = _refusal(answer)
	if not report.check(_("Accepted by {0}").format(doc.provider), not refused, refused or _("accepted")):
		return report.as_dict()
	report.check(_("Extensions"), True, str(sum(1 for user in _rows(answer) if (user.get("extension") or "").strip())))
	siblings = [account for account in resolve.tenant_of(name) if account != name]
	report.check(_("Same provider login as"), True, ", ".join(siblings) or _("no other account"))
	report.check(_("Webhook token"), bool(doc.webhook_token), _("set") if doc.webhook_token else _("not set, so this account's calls cannot be received"))
	return report.as_dict()


def _enabled_accounts() -> list:
	"""The accounts a fetch may ask through: the one list the picker offers and both fetches enforce."""
	return frappe.get_all(resolve.ACCOUNT_DOCTYPE, filters={"enabled": 1}, pluck="name", order_by="name")  # authz-ok: tier-b — names only, every caller first checks write on the record that fetches


def _fetch_account(account):
	"""The account to ask through, refused when it is not enabled: a disabled account is never asked, however it is named."""
	if account not in _enabled_accounts():
		frappe.throw(_("{0} is not an enabled telephony account.").format(account), title=_("Account disabled"))
	return frappe.get_doc(resolve.ACCOUNT_DOCTYPE, account)


def _answer(rows, refused, account=None) -> dict:
	"""The one shape both dialogs read: every row `{group, name, taken, taken_by}`, and what the provider would not answer."""
	return {
		"account": account,
		"rows": sorted(rows, key=lambda r: (r["group"] or "~", r["taken"], r["name"])),
		"refused": refused,
	}


def _departments_by_agent(adapter, account) -> dict:
	"""The provider's agent id -> the departments it sits in, on this account."""
	found = {}
	for department in _rows(adapter.get_departments(account)):
		for member in department.get("agents") or []:
			found.setdefault(member.get("eid"), []).append(department.get("name") or "")
	return found


def _rows(response) -> list:
	"""The list inside a provider answer, empty when it refused — `_refusal` is what names the reason."""
	if isinstance(response, list):
		return response
	if isinstance(response, dict) and isinstance(response.get("data"), list):
		return response["data"]
	return []


def _refusal(response):
	"""How the provider worded its refusal, or None when it answered."""
	if isinstance(response, dict) and response.get("status_code") is not None:
		return str(response.get("message") or response.get("status_code"))
	return None
