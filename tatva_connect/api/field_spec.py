"""The contract layer: ONE declaration, read by discovery AND ingestion.

A resource declares its payload contract as `FieldSpec`s. `describe` turns them into what `*_schema`
advertises; `collect` turns a caller's payload into what the write path may apply. Both iterate the
SAME specs, so what a partner is told and what a partner may send cannot drift — not because they are
kept in step, but because neither one owns a field list.

The three rules:

  * `collect` iterates SPECS, never `data.keys()`. A key the caller sends that no spec declares is
    dropped in silence — that is the mechanism by which a field cannot be injected. It does not throw:
    an undeclared key is not an error, it is simply not part of the contract.
  * `read_only` is a computed column: OUTPUT_ONLY to `describe`, invisible to `collect`. Discoverable,
    never writable, no matter what the caller sends.
  * `describe` reads LIVE meta. A `target` that is not a real column throws. Degrading to "Data" is how
    a schema starts advertising a column that no longer exists.
  * `collect` holds every value to the type `describe` PUBLISHES for it (`_base.cast_declared`), so the
    declaration is the enforcement for the type too and not only for the field list. What a value of
    that type MEANS is still the column's business — see `_base.TYPE_VALUE_DECIDED_ELSEWHERE`.

`target=None` means the field is not a column: the resource resolves it itself. `mobile_no` finds a
lead; `created_at` backdates `creation`, which is a framework default field (not a docfield) and is in
RESERVED_FIELDS — so targeting it would BOTH throw here and force OUTPUT_ONLY on a field a partner
legitimately sends. It stays target-less, the backdate stays per-resource write logic, and its type is
the one thing a spec must declare, because meta has no column to answer with.

`target_doctype=None` means the resource's own doctype — the one passed to `describe`. It is per-spec
ONLY where a section genuinely lands on another doctype; restating the same doctype on every row is
what this layer exists to stop.

This is the declaration and the two readers, and nothing more. Resolution stays per-resource: it
genuinely differs (leads carry child arrays, activities route promoted-vs-payload, files carry bytes).
"""
from typing import NamedTuple

import frappe
from frappe import _

from tatva_connect.api._base import BEHAVIOR_OUTPUT_ONLY, cast_declared, field_descriptor, throw_field


class FieldSpec(NamedTuple):
	"""One field's public contract. Immutable, positional, and the only place it is declared."""

	fieldname: str  # the public name the caller sends
	label: str
	target: str | None = None  # the column it lands on; None = not a column
	target_doctype: str | None = None  # None = the resource's own doctype
	required: bool = False  # the API's contract, which may be looser than the doctype's reqd
	supplied: bool = False  # the resource fills this when omitted — the only way a spec stays optional over a mandatory column
	read_only: bool = False  # computed; never accepted from a caller
	allowed_values: tuple | None = None  # the partner's vocabulary; None = a Select's own options
	fieldtype: str | None = None  # ONLY for a non-column, which meta cannot type; declaring both throws


def _docfield(spec, doctype):
	"""The live docfield a spec targets, or None when it is not a column. A dead target throws."""
	if not spec.target:
		return None
	if spec.fieldtype:
		frappe.throw(
			_("{0}: declares target {1} AND fieldtype — live meta already types a column")
			.format(spec.fieldname, spec.target)
		)
	dt = spec.target_doctype or doctype
	field = frappe.get_meta(dt).get_field(spec.target) if dt else None
	if not field:
		frappe.throw(_("{0}: target {1} is not a column of {2}").format(spec.fieldname, spec.target, dt))
	return field


def _published_type(spec, field):
	"""The ONE type this spec publishes: live meta for a column, the spec's own for a non-column, Data
	for a non-column nobody typed. `describe` advertises it and `collect` enforces it, from here."""
	return field.fieldtype if field else (spec.fieldtype or "Data")


def _vocabulary(spec, field):
	"""A field's allowed values: declared wins, else a Select's own options. Nothing else has any."""
	if spec.allowed_values:
		return list(spec.allowed_values)
	if field and field.fieldtype == "Select":
		return [v for v in (field.options or "").split("\n") if v] or None
	return None


def describe(specs, doctype=None):
	"""The specs as partner-facing descriptors — what a `*_schema` endpoint advertises.

	A column's type comes from live meta, never from the spec: the doctype already knows, and a second
	copy is a second brain — so declaring both a target and a fieldtype throws. Only a NON-column may
	declare its fieldtype, because there meta has nothing to answer with. `doctype` is the resource's
	own, used for every spec that names no other.

	The vocabulary is the exception, and the reason `allowed_values` exists: `direction` lands on a
	column holding Incoming/Outgoing while partners speak Inbound/Outbound. A declared vocabulary
	therefore suppresses the native options too — if a resource renames the values, the internal ones
	are not the partner's business. The spec declares the vocabulary; translating it stays per-resource."""
	out = []
	for spec in specs:
		field = _docfield(spec, doctype)
		d = field_descriptor(
			spec.fieldname,
			spec.label,
			_published_type(spec, field),
			spec.required and not spec.read_only,
			None if spec.allowed_values else (field.options if field else None),
			_vocabulary(spec, field),
		)
		if spec.read_only:
			d["behavior"] = BEHAVIOR_OUTPUT_ONLY  # computed: discoverable, never writable
		out.append(d)
	return out


def collect(specs, data, doctype, creating=False):
	"""The caller's payload as `{target: value}`, every value in the type this contract PUBLISHES —
	what a write path may apply.

	Iterates SPECS, so an undeclared key is dropped and a caller can never inject. A read-only spec is
	skipped however hard the caller pushes, and a spec with no target is the resource's own business.

	The type is `describe`'s own answer, read through `_published_type`, so discovery and ingestion agree
	about the TYPE and not merely about the field list — the rule and its wording live once, in
	`_base.cast_declared`. A target-less spec is still held to its declared type here even though nothing
	is routed for it: the enforcement is the refusal, not the routing, and `created_at` publishes a
	Datetime whoever applies it.

	A refusal names the PUBLIC fieldname. `started_at` lands on `start_time`, and a caller has never
	heard of `start_time`."""
	out = {}
	sent = set()
	for spec in specs:
		if spec.read_only or spec.fieldname not in data:
			continue
		value = data[spec.fieldname]
		# An empty string is "not sent", never "erase this" — the rule `partner._collect` holds for a lead.
		if isinstance(value, str) and not value.strip():
			continue
		value = cast_declared(doctype, spec.fieldname, value,
		                      fieldtype=_published_type(spec, _docfield(spec, doctype)))
		sent.add(spec.fieldname)
		if not spec.target:
			continue
		out[spec.target] = value
	if creating:
		# What was SENT, not what was ROUTED: a target-less spec never lands in `out` by design (it is the
		# resource's own business, like `filename` reaching file_manager.save), so reading `out` alone
		# called every required one of them missing and refused a correct call.
		missing = [s.fieldname for s in specs
		           if s.required and not s.read_only and not s.supplied and s.fieldname not in sent]
		if missing:
			throw_field(_(
				"Required and not sent: {0}. Read `required` from the schema response and send every "
				"field it marks true."
			).format(", ".join(f"`{m}`" for m in missing)), missing)
	return out
