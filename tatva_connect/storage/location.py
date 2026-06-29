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


# Frappe models "the record this one is about" as a Dynamic Link field whose `options`
# names the companion doctype field — `reference_doctype` (core/CRM) or `reference_type`
# (ToDo). We follow that link generically from meta, so we never hardcode the per-doctype
# value field (reference_name vs reference_docname vs …).
_REF_DOCTYPE_FIELDS = ("reference_doctype", "reference_type")


def _reference_parent(dt, nm):
	"""The (doctype, name) this record points at via its canonical 'reference' Dynamic Link,
	or None when it has none / it's unset / the target is gone. Read straight off the meta,
	so every reference convention (Communication, Comment, ToDo, CRM Task/Note/Call Log) works
	without a hardcoded field name."""
	for df in frappe.get_meta(dt).get("fields", {"fieldtype": "Dynamic Link"}):
		if df.options not in _REF_DOCTYPE_FIELDS:
			continue
		row = frappe.db.get_value(dt, nm, [df.options, df.fieldname], as_dict=True)
		if not row:
			return None
		ref_dt, ref_nm = row.get(df.options), row.get(df.fieldname)
		if ref_dt and ref_nm and frappe.db.exists(ref_dt, ref_nm):
			return ref_dt, ref_nm
	return None


def resolve_owner(attached_dt, attached_nm):
	"""Walk a File's attachment up to the ROOT business record — the lead / deal / ticket it
	ultimately belongs to — so EVERY file for that record (its emails, comments, tasks, notes,
	calls, and direct attachments) groups in one folder. Each hop follows the record's canonical
	'reference' Dynamic Link; roots (lead/deal/ticket) carry none, so the walk stops there.
	Returns (root_doctype, root_name, app): the root's owning app when it's a business app, else
	'platform' (e.g. a Contact, which is many-to-many and has no single owner)."""
	dt, nm = attached_dt, attached_nm
	seen = set()
	while dt and nm and (dt, nm) not in seen:
		seen.add((dt, nm))
		parent = _reference_parent(dt, nm)
		if not parent:
			break                                    # no parent reference -> dt/nm is the root
		dt, nm = parent
	if not dt or not nm:
		return None, None, DEFAULT_APP               # unattached / unresolvable -> platform
	app = frappe.get_doctype_app(dt)
	return dt, nm, (app if app in _BUSINESS_APPS else DEFAULT_APP)


def is_new_scheme(blob_key) -> bool:
	"""New keys always start with a known app segment; old keys never do (scrubbed
	doctype or a hash) — so old-vs-new routes straight off the key, no per-file field."""
	return bool(blob_key) and blob_key.split("/")[0] in (_BUSINESS_APPS | {DEFAULT_APP})
