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

from tatva_connect.api._base import BEHAVIOR_OUTPUT_ONLY, field_descriptor


class FieldSpec(NamedTuple):
	"""One field's public contract. Immutable, positional, and the only place it is declared."""

	fieldname: str  # the public name the caller sends
	label: str
	target: str | None = None  # the column it lands on; None = not a column
	target_doctype: str | None = None  # None = the resource's own doctype
	required: bool = False  # the API's contract, which may be looser than the doctype's reqd
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
			field.fieldtype if field else (spec.fieldtype or "Data"),
			spec.required and not spec.read_only,
			None if spec.allowed_values else (field.options if field else None),
			_vocabulary(spec, field),
		)
		if spec.read_only:
			d["behavior"] = BEHAVIOR_OUTPUT_ONLY  # computed: discoverable, never writable
		out.append(d)
	return out


def collect(specs, data):
	"""The caller's payload as `{target: value}` — what a write path may apply.

	Iterates SPECS, so an undeclared key is dropped and a caller can never inject. A read-only spec is
	skipped however hard the caller pushes, and a spec with no target is the resource's own business."""
	out = {}
	for spec in specs:
		if spec.read_only or not spec.target:
			continue
		if spec.fieldname in data:
			out[spec.target] = data[spec.fieldname]
	return out
