# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""What this platform INGESTED — derived from the ingest configuration, never from a typed source list.

THE DEFECT THIS DELETES. Every inbound path stamps `CRM Lead.source` from an operator-configured row:
the partner API and the Facebook sync from a `CRM Lead API Mapping` (`api/partner.py` and
`lead_sync/source.py` both hand `_upsert_one` a mapping's `source`), a web or intake form from a
`CRM Intake Form` (`intake/intake.py`). The desk instead carried four literals — `Partner API`,
`FB Lead Ads`, `Website`, `Landing Page MR Form` — copied into six filters. Measured on this site,
three of the four are declared by NO config: they are LeadSquared-era rows being counted as arrivals,
while six sources the config really does declare were counted by nothing. Over- and under-stated at
once, and silent either way, because a literal that matches nothing raises no error.

ARRIVALS ARE NOT FILTERED AT ALL. A count of leads is a count of leads: narrowing it to a source set —
typed OR derived — hides the rest and states no reason for doing so. The first repair here swapped four
literals for a derived set and still showed 76 of 4,934 leads in thirty days, which is a worse lie than
the one it replaced because it looks considered. The windows are catch-all; the SPLITS carry the source
dimension, and every split accounts for every lead, so the slices sum to the total.

The declared set is still asked, on every read, of the rows that stamp it — but only to LABEL a lead's
lane, never to decide whether it counts. A mapping added tomorrow is labelled tomorrow with no edit here.

There is no by-source function here either: grouping leads by source is a NATIVE Group By chart, and a
second implementation of it could only drift from frappe's.

WHAT IS DELIBERATELY NOT SPLIT. The Facebook sync and the partner API both route through a mapping, so
`source` cannot tell them apart and this module does not pretend to: the split it offers is the one the
configuration actually declares — contracts against intake forms.
"""

import frappe

MAPPING_DT = "CRM Lead API Mapping"
INTAKE_DT = "CRM Intake Form"
LEAD_DT = "CRM Lead"


def _values(doctype, filters=None):
	return {v for v in frappe.get_all(doctype, filters=filters or {}, pluck="source") if v}


def declared_sources() -> dict:
	"""The sources the live ingest configuration stamps, by the config that stamps them.

	Only ENABLED rows: a disabled contract or intake form accepts nothing, so its source is not an inbound lane.
	"""
	return {"contract": _values(MAPPING_DT, {"enabled": 1}), "intake": _values(INTAKE_DT, {"enabled": 1})}


def _all_declared() -> list:
	sets = declared_sources()
	return sorted(sets["contract"] | sets["intake"])


def _count(sources, days=None) -> int:
	"""Leads in the window. `sources=None` counts every lead, which is what an arrival figure is."""
	filters = {} if sources is None else {"source": ["in", list(sources)]}
	if sources is not None and not sources:
		return 0
	if days is not None:
		filters["creation"] = [">=", frappe.utils.add_days(frappe.utils.today(), -days)]  # frappe's own "last N days"
	return _leads(filters)


def _leads(filters) -> int:
	"""A lead count through the permission layer, the same `get_list` a native Number Card counts with."""
	rows = frappe.get_list(LEAD_DT, fields=[{"COUNT": "*", "as": "result"}], filters=filters)
	return int(rows[0]["result"]) if rows else 0


def by_path(days=30) -> dict:
	"""Which door every lead came through. The slices SUM TO THE TOTAL, which is what makes it a split.

	`Other` is the rest — a migrated record, a rep typing a source, a blank. It is by far the largest
	slice on a site carrying history, and hiding it is how a lead count came to show 76 of 4,934.
	"""
	sets = declared_sources()
	contracts, intake = _count(sets["contract"], days), _count(sets["intake"] - sets["contract"], days)
	return {"Contracts": contracts, "Intake forms": intake,
	        "Other": _count(None, days) - contracts - intake}


def _card(value: int) -> dict:
	return {"value": int(value), "fieldtype": "Int"}


def _gate():
	"""Reads leads, so it asks the lead gate — never a role name, which would be a second matrix."""
	frappe.has_permission(LEAD_DT, "read", throw=True)


@frappe.whitelist()
def card_today(filters=None) -> dict:
	"""Every lead created today, on the calendar day, with no source filter."""
	_gate()
	return _card(_leads({"creation": [">=", frappe.utils.today()]}))


@frappe.whitelist()
def card_7d(filters=None) -> dict:
	"""Every lead created in the last seven days."""
	_gate()
	return _card(_count(None, 7))


@frappe.whitelist()
def card_30d(filters=None) -> dict:
	"""Every lead created in the last thirty days."""
	_gate()
	return _card(_count(None, 30))


@frappe.whitelist()
def card_sources_declared(filters=None) -> dict:
	"""How many inbound lanes the configuration declares. Zero means nothing can arrive at all."""
	_gate()
	return _card(len(_all_declared()))
