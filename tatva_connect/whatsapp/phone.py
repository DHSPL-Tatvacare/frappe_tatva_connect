"""Phone canonicalisation for the WhatsApp channel.

One rule, one code path: numbers are stored canonical E.164 (`+<digits>`) and reduced to comparable digits
by `phone.match_digits` when two spellings must be matched. A provider's subscriber id is bare digits and
stock leads were stored as `+91-XXXXXXXXXX`; without this they would never match.

The shaping is Google's libphonenumber, which Frappe already depends on (`frappe/pyproject.toml`) and
already wraps (`frappe.utils.validate_phone_number_with_country_code`). It is used rather than arithmetic on
digits because it carries every country's real numbering plan: it knows `1111111111` is not an Indian
mobile, that India strips a leading trunk `0`, and that `+39 06 …` is a Rome landline. The hand-rolled
version this replaces counted digits, so `09876543210` — what browser autofill hands you — became
`+09876543210` and matched nothing, ever.
"""
import frappe
import phonenumbers
from frappe import _
from frappe.utils import cstr
from frappe.utils.caching import redis_cache


def region_default() -> str | None:
	"""The country a number carrying no `+` is read as — Frappe's own System Settings country.

	Read from the operator's setting rather than named here, so opening a second country is one setting and
	not a code change. None when the site declares no country: a bare number then has nothing to be
	interpreted against and is refused, which is the truth rather than a guess."""
	country = frappe.db.get_single_value("System Settings", "country")
	code = frappe.get_cached_value("Country", country, "code") if country else None
	return code.upper() if code else None


def to_e164(number: str, region: str | None = None, fieldname: str | None = None) -> str:
	"""THE stored form of a phone number: `+<country code><national number>`, or a refusal.

	A number that starts with `+` carries its own country and is stored as it says — a Saudi, US or Italian
	number is never rewritten to India. `region` is consulted ONLY for a number with no `+`, and defaults to
	the site's country: a bare number genuinely does not contain a country, so something must supply one,
	and a caller that knows better (an intake form with a country picker) passes it.

	Refuses what is not a real number anywhere. Blank stays blank — "no number given" is not "a bad number".

	Callers whose job is to LOOK SOMETHING UP rather than store it catch this: a search for a malformed
	number should find nothing, not fail."""
	raw = cstr(number).strip()
	if not raw:
		return ""
	try:
		parsed = phonenumbers.parse(raw, region or region_default())
		if phonenumbers.is_valid_number(parsed):
			return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
	except phonenumbers.NumberParseException:
		pass
	# Frappe's own wording for this refusal, so the message a rep sees here is the message core gives.
	frappe.throw(
		_("Phone Number {0} set in field {1} is not valid.").format(raw, fieldname or _("Phone")),
		title=_("Invalid Phone Number"),
	)


@frappe.whitelist()
@redis_cache(ttl=24 * 60 * 60)
def dial_codes() -> list[dict]:
	"""The country codes a phone input offers, and which one it opens on.

	Both halves come from something that already knows: the country LIST is Frappe's own `Country` table,
	and each dial code is libphonenumber's, so nothing here is a table of numbers that goes stale when a
	country changes its plan. A country the library does not recognise is simply not offered.

	Cached in Redis for the SITE, not per user or per request — the answer is the same for everyone and
	changes only when an operator edits the Country table or the site's country. 250 rows and 250 library
	lookups are then paid once a day instead of once per person who opens the picker. The TTL is the
	self-heal: an operator's edit shows up within a day without anyone clearing anything.

	The picker is an aid for TYPING, not a field: it prefixes `+<code>` so the rep does not have to know
	they must. Nothing stores it — once the number reads `+966…` it carries its own country for ever.

	`region` is the identity, not `dial`: two dozen countries answer to `+1`. `primary` is the region
	libphonenumber names for a calling code, so a stored `+1…` reads back as the United States."""
	default = region_default()
	out = []
	for row in frappe.get_all("Country", fields=["name", "code"], order_by="name"):  # authz-ok: public reference data, no business record — the country list every phone input needs
		code = (row.code or "").upper()
		dial = phonenumbers.country_code_for_region(code) if code else 0
		if not dial:
			continue
		out.append({"country": row.name, "region": code, "dial": f"+{dial}",
					"default": int(code == default),
					"primary": int(code == phonenumbers.region_code_for_country_code(dial))})
	return out


def sweep(doctype: str = "CRM Lead", field: str = "mobile_no") -> dict:
	"""Bring existing rows to the stored form, and report the ones that cannot get there.

	A repair tool meets bad data by definition, so a refusal is an ANSWER here, not a failure: the row is
	counted and named for a human instead of stopping the sweep. Direct DB writes (update_modified=False)
	so no controller or notification fires for a spelling change."""
	rows = frappe.get_all(doctype, filters={field: ["is", "set"]}, fields=["name", field])
	changed, refused = 0, []
	for r in rows:
		old = r.get(field) or ""
		try:
			new = to_e164(old, fieldname=field)
		except frappe.ValidationError:
			frappe.clear_last_message()
			refused.append({"name": r.name, field: old})
			continue
		if new != old:
			frappe.db.set_value(doctype, r.name, field, new, update_modified=False)
			changed += 1
	frappe.db.commit()
	return {"scanned": len(rows), "changed": changed, "refused": refused}
