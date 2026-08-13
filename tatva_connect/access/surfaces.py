"""The ONE surface-visibility rule — which whole screens exist for this caller.

Every surface answers the SAME two-part question, and nothing else:

    visible = permitted() AND live()

`permitted()` is `frappe.has_permission(<the surface's doctype>)`, ALWAYS. Never a role literal: the
DocPerms are the declaration and roles are only how they are granted, so a role list here would be a
second answer to the same question and would drift the day an operator adds a role. The ONE exception is
Near Me, which owns no doctype — its rule already lives in `near_me/api._can_access`, so this module calls
that rule rather than spelling a second copy of it.

`live()` is the operator's own toggle for the surface: an automation switch for Near Me and Workflows, and
for Deals the question "does any business line this caller is entitled to actually sell deals yet".

Deals, Contacts and Organizations are ONE product — a contact and an organization exist to be sold to — so
all three ride that same liveness answer, read ONCE per call and shared. Adding the two extra surfaces
therefore costs zero extra queries, which is the whole reason this map is affordable on every boot.

The answer rides the CRM's EXISTING boot payload (`hooks.crm_surfaces` -> `crm/www/crm.py`), so the menu is
correct on first paint at the cost of no extra HTTP request. That is the whole point: this replaced three
boot-time gate calls and the menu pop-in they caused. Every branch fails CLOSED — a surface that cannot
answer is hidden, and can never break the boot payload that carries it.
"""
import frappe

from tatva_connect import automation
from tatva_connect.access import entitlement
from tatva_connect.near_me import api as near_me_api
from tatva_connect.taxonomy import grain

WORKFLOW_SURFACE_SWITCH = "Workflow::Authoring::surface"

# The surfaces the Deals liveness answer governs, each named by the doctype whose permission it asks.
DEAL_SURFACES = (("deals", "CRM Deal"), ("contacts", "Contact"), ("organizations", "CRM Organization"))


def my_surfaces() -> dict:
	"""Which surfaces this caller may see. Not whitelisted — it rides boot, it is never fetched."""
	surfaces = {
		"near_me": _answer(_near_me),
		"workflows": _answer(_workflows),
	}
	# ONE liveness read, shared by all three. `and` short-circuits, so a dead line asks no permission at all.
	live = _answer(_deals_live)
	for key, doctype in DEAL_SURFACES:
		surfaces[key] = live and _answer(lambda dt=doctype: frappe.has_permission(dt, "read"))
	return surfaces


def _answer(rule) -> bool:
	"""One gate's answer, fail-closed: a rule that raises hides its menu item, it never breaks boot."""
	try:
		return bool(rule())
	except Exception:
		frappe.log_error(title="Surface gate failed", message=frappe.get_traceback())
		return False


def _near_me() -> bool:
	"""Near Me's own rule, asked of Near Me — one rule, one home, so the two can never drift apart."""
	return near_me_api._can_access()


def _workflows() -> bool:
	"""May author workflows, and the authoring screen is switched on for this site."""
	return bool(
		frappe.has_permission("CRM Workflow", "read")
		and automation.is_enabled(WORKFLOW_SURFACE_SWITCH)
	)


def deals_enabled(vertical: str = "") -> bool:
	"""Is this business line armed to sell? THE one fact every deal-shaped surface asks.

	`CRM Vertical.deals_enabled` governs everything deal-related, so the sidebar's Deals/Contacts/
	Organizations, the workflow subject picker and the Smart Views base object all come through here: a
	line that does not sell is absent from all of them, or present in all of them, never some of each.

	A BLANK vertical is a WILDCARD, never a miss — the house rule `_entitled_verticals` already keeps —
	so an unscoped question asks whether ANY line sells. Uncached: arming a line takes effect at once."""
	return _lines_sell({vertical} if vertical else set())


def _lines_sell(verticals: set) -> bool:
	"""Does any of these lines sell? The ONE read of `deals_enabled`; an empty set means every line.

	One indexed read either way — the narrowed form filters on the master's own primary key."""
	filters = {"deals_enabled": 1}
	if verticals:
		filters["name"] = ["in", sorted(verticals)]
	return bool(frappe.db.count(grain.master("vertical"), filters))


def _deals_live() -> bool:
	"""Has at least one business line this caller works been armed to sell? The liveness half of Deals,
	Contacts and Organizations alike — computed once per call so two more surfaces cost nothing.

	The PER-USER question, and the only one that is: it narrows by the caller's entitlement. A workflow or
	a Smart View declares its own grain instead, so those ask `deals_enabled(vertical)` — same fact, same
	reader, different subject."""
	return _lines_sell(_entitled_verticals())


def _entitled_verticals() -> set:
	"""The verticals the caller is entitled to, or the empty set meaning "no explicit list — any line".

	Read from the entitlement brain, never resolved a second time here. A System Manager holds the
	ALL_GRAINS sentinel and a partial grain leaves its vertical axis BLANK, which is a wildcard — both
	are "no list to narrow by", and both are answered by the same single count over every line.
	"""
	grains = entitlement.entitled_grains()
	if grains == entitlement.ALL_GRAINS:
		return set()
	axis = grain.AXES.index("vertical")
	verticals = {(g[axis] or "") for g in grains}
	return set() if "" in verticals else verticals


