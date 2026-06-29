# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""Where a blob lives — env + creds from `site_config`, owner from a walk-up.

Container identity and credentials are a property of the BOX, not the data: they
come from `site_config` (never the DB), so a prod DB restored into uat can never
resolve — let alone delete — prod's bytes. The blob path stamps the owning app +
business record so the container browses sensibly and an email attachment lands in
the same folder as the lead it belongs to.
"""

import frappe
from frappe import _

ENV_KEY = "tatva_storage_env"
CONN_KEY = "tatva_storage_connection_string"
LEGACY_KEY = "tatva_storage_legacy_container"

DEFAULT_APP = "platform"
_BUSINESS_APPS = {"crm", "helpdesk", "wiki", "lms"}


def env() -> str:
	"""The environment container (`dev`/`uat`/`prod`) for this box. Fail-closed: no key,
	no Azure op — files stay local rather than default into a shared/prod space."""
	value = frappe.conf.get(ENV_KEY)
	if not value:
		frappe.throw(_("Set {0} in site_config.").format(ENV_KEY))
	return value


def connection_string() -> str:
	"""The Azure connection string for this box (from site_config, never the DB)."""
	value = frappe.conf.get(CONN_KEY)
	if not value:
		frappe.throw(_("Set {0} in site_config.").format(CONN_KEY))
	return value


def legacy_container() -> str | None:
	"""The pre-migration container (prod-only). Absent on uat/dev — so a restored prod
	legacy row is unresolvable there and can never touch prod's bytes."""
	return frappe.conf.get(LEGACY_KEY)


def resolve_owner(attached_dt, attached_nm):
	"""Walk from a File's attachment up to the owning business record.
	Generic: follows any doctype that carries reference_doctype/reference_name
	(Communication, ToDo, Comment, …) — detected via meta, not a hardcoded list."""
	dt, nm = attached_dt, attached_nm
	seen = set()
	while dt and (dt, nm) not in seen:
		seen.add((dt, nm))
		app = frappe.get_doctype_app(dt)
		if app in _BUSINESS_APPS:
			return dt, nm, app                       # a business record — stop here
		if nm and frappe.get_meta(dt).has_field("reference_doctype"):
			ref = frappe.db.get_value(dt, nm, ["reference_doctype", "reference_name"], as_dict=True)
			if ref and ref.reference_doctype and ref.reference_name:
				dt, nm = ref.reference_doctype, ref.reference_name
				continue                             # walk up
		break
	return None, None, DEFAULT_APP                   # unresolvable -> platform


def is_new_scheme(blob_key) -> bool:
	"""New keys always start with a known app segment; old keys never do (scrubbed
	doctype or a hash) — so old-vs-new routes straight off the key, no per-file field."""
	return bool(blob_key) and blob_key.split("/")[0] in (_BUSINESS_APPS | {DEFAULT_APP})
