"""Read-only provider listings for the authoring inspector.

WHY THIS EXISTS AT ALL: the agent picker has to show the agents that really exist on the account, and the
only way to know them is to ask the provider. The api key is a Password field, so the browser can never
hold it and must not — these methods read it server-side with `get_password` and return only the agent's
own public shape. Nothing here can place a call: both endpoints are GETs against the provider.

The provider is resolved from the ACCOUNT row, never named here — the same rule the webhook spine follows,
so a second voice vendor is a registry line and not an edit to this file.
"""
import frappe

from tatva_connect.channels import resolve

ACCOUNT_DOCTYPE = "CRM AI Voice Account"


def connection_for(account_name):
	"""The provider connection dict for one account: {api_key, base_url, from_phone}.

	The ONE place the key is read. `_deliver_voice` builds the same dict for the dial path; both go
	through `get_password`, and neither ever hands it further than the adapter it calls.
	"""
	account = frappe.get_cached_doc(ACCOUNT_DOCTYPE, account_name)
	return {
		"api_key": account.get_password("api_key", raise_exception=False),
		"base_url": account.base_url,
		"from_phone": account.from_phone,
	}


def adapter_for(account_name):
	"""The adapter module this account's provider field names."""
	return resolve.adapter_for(account_name, ACCOUNT_DOCTYPE)


def _listing(account, reader, title):
	"""One shape for every provider picklist: `{options: [{value, label}], error}`.

	The inspector draws ONE control for all of them, so the shape is decided here rather than per list.
	"error" is not an empty list: "this account has no numbers" and "we could not reach the provider" are
	different answers, and an author shown an empty dropdown for the second goes looking in the wrong place.
	"""
	if not account:
		return {"options": [], "error": None}
	frappe.has_permission(ACCOUNT_DOCTYPE, "read", doc=account, throw=True)
	try:
		rows = reader(adapter_for(account), connection_for(account))
	except Exception as e:
		frappe.log_error(title=title, message=frappe.get_traceback())
		return {"options": [], "error": str(e)}
	return {
		"options": [
			{
				"value": row["id"],
				# The id is the fallback label: a nameless agent must still be selectable, not blank.
				"label": " · ".join(p for p in (row.get("name"), row.get("status")) if p) or row["id"],
			}
			for row in rows
			if row.get("id")
		],
		"error": None,
	}


@frappe.whitelist()
def list_agents(account=None):
	"""Every agent on one voice account. Permission-gated on the account: an author who may not read the
	account may not enumerate the agents configured on it."""
	return _listing(account, lambda a, c: a.list_agents(c), "voice: agent listing failed")


@frappe.whitelist()
def list_phone_numbers(account=None):
	"""The caller-id numbers this account owns, for the From-number picker."""
	return _listing(account, lambda a, c: a.list_phone_numbers(c), "voice: number listing failed")


@frappe.whitelist()
def get_agent(account, agent_id):
	"""One agent in full, for the prompt view. Same gate, same read-only path."""
	frappe.has_permission(ACCOUNT_DOCTYPE, "read", doc=account, throw=True)
	if not agent_id:
		return {"agent": None, "error": None}
	adapter = adapter_for(account)
	try:
		return {"agent": adapter.get_agent(connection_for(account), agent_id), "error": None}
	except Exception as e:
		frappe.log_error(title="voice: agent fetch failed", message=frappe.get_traceback())
		return {"agent": None, "error": str(e)}


@frappe.whitelist()
def agent_slots(template, account=None):
	"""The placeholders THIS agent speaks — the rows the node's Agent Values grid offers.

	`template` is the agent id, named that way because it is the ONE argument every slots control passes;
	`account` rides alongside as a declared sibling arg. Same contract as `sends.template_slots`: the grid
	asks the provider what the call really needs, so an author fills slots that exist rather than typing
	names that match nothing. An unfilled slot is not a blank on a screen — it is the words
	"Hi customer_name" said out loud to a patient.
	"""
	if not (account and template):
		return []
	frappe.has_permission(ACCOUNT_DOCTYPE, "read", doc=account, throw=True)
	try:
		return adapter_for(account).agent_variables(connection_for(account), template)
	except Exception:
		# A provider we cannot reach must not blank a grid the author has already filled in.
		frappe.log_error(title="voice: agent slot listing failed", message=frappe.get_traceback())
		return []


# An agent's placeholders change when someone edits the prompt on the provider — rarely, and never
# mid-cohort. Ten minutes is short enough that an edit shows up within one coffee break and long enough
# that a 3,000-lead cohort asks the provider once instead of 3,000 times. An explicit TTL, never a
# permanent key: a cache that cannot expire is a cache that needs a human to fix it.
_SLOT_CACHE_TTL = 600


def agent_variables_for(account, agent_id):
	"""Server-side slot names for the SEND path — the same answer the grid got, asked without a session.

	CACHED, because this runs on EVERY send. Uncached it put a synchronous provider round-trip in front of
	every dial, so one cohort became one API call per lead before a single phone rang.
	"""
	key = f"voice:agent-slots:{account}:{agent_id}"
	cached = frappe.cache().get_value(key)
	if cached is not None:
		return cached
	names = adapter_for(account).agent_variables(connection_for(account), agent_id)
	frappe.cache().set_value(key, names, expires_in_sec=_SLOT_CACHE_TTL)
	return names


# NB: the account form's webhook URL banner is `webhooks.urls.get_account_webhook_urls`, already
# whitelisted and already what the shared Desk helper calls. There is deliberately no voice copy of it.
