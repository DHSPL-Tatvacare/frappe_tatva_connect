# TEMPORARY — migration reconciliation demo, remove before prod. See REMOVE-ME.md
"""The gate. Config enables this tool, not code.

Production carries none of the `migration_check_*` keys, so this module refuses there even if the
code rides a merge. That is the primary protection; login and the role check sit on top of it.

Credentials are PER GRAIN. Each LeadSquared account has its own keys and each partner user its own
token scoped to one vertical+group, so the operator's choice selects a whole credential set — not
just a label. A grain with no configured credentials is not offered.
"""

import frappe
from frappe import _

from tatva_connect.migration_check import constants as C

# Roles allowed in. Operator-configurable via `migration_check_roles`, because who validates a
# migration is a business decision, not a code one. System Manager is always allowed so a site
# cannot lock its own administrators out of the tool.
DEFAULT_ROLES = ("System Manager",)
_REQUIRED = ("lsq_host", "lsq_access_key", "lsq_secret_key", "partner_token")


class NotEnabled(frappe.PermissionError):
	"""Raised when the tool is off, or the caller may not use it."""


def _site_enabled() -> None:
	"""Raised, not thrown: the page controllers catch this to render a 404 rather than a message.
	A site that has not switched the tool on should not advertise that it exists."""
	conf = frappe.conf
	if not conf.get("migration_check_enabled"):
		raise NotEnabled(_("Migration Check is not enabled on this site."))
	if frappe.local.site not in (conf.get("migration_check_allowed_sites") or []):
		raise NotEnabled(_("Migration Check is not enabled on this site."))


def _accounts() -> dict:
	"""Configured grains only. A grain missing any credential is treated as not configured."""
	raw = frappe.conf.get("migration_check_accounts") or {}
	out = {}
	for slug, cfg in raw.items():
		if slug in C.GRAINS and all((cfg or {}).get(k) for k in _REQUIRED):
			out[slug] = cfg
	return out


def roles() -> tuple:
	"""Every role that may use the tool. System Manager is never removable."""
	configured = frappe.conf.get("migration_check_roles") or []
	if isinstance(configured, str):
		configured = [configured]
	return tuple(dict.fromkeys([*DEFAULT_ROLES, *(r for r in configured if r)]))


def may_use() -> bool:
	return bool(set(roles()) & set(frappe.get_roles()))


def assert_permitted() -> None:
	"""Enabled on this site, a real logged-in user, holding the required role, with a grain set up."""
	_site_enabled()

	if frappe.session.user == "Guest":
		raise frappe.PermissionError(_("Please log in."))

	if not may_use():
		frappe.throw(_("You do not have access to Migration Check."), NotEnabled)

	if not _accounts():
		frappe.throw(_("Migration Check has no configured grains on this site."), NotEnabled)


def available_grains() -> list[dict]:
	"""What the operator may pick — configured grains only, in label order."""
	configured = _accounts()
	return [c for c in C.choices() if c["slug"] in configured]


def credentials(slug: str) -> frappe._dict:
	"""The credential set for one grain. Raises if that grain is not configured here."""
	cfg = _accounts().get(slug)
	if not cfg:
		frappe.throw(_("That grain is not configured on this site."), NotEnabled)
	return frappe._dict(
		lsq_host=cfg["lsq_host"],
		lsq_access_key=cfg["lsq_access_key"],
		lsq_secret_key=cfg["lsq_secret_key"],
		partner_token=cfg["partner_token"],
	)


def is_available() -> bool:
	"""For the page template — render the form, or behave as if the page is not there."""
	try:
		assert_permitted()
		return True
	except (frappe.PermissionError, NotEnabled):
		return False
