# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""Which words in a typed query are values the search index can actually filter on.

The index filters ONLY on its own metadata columns (`CRMLeadSearch.INDEX_SCHEMA["metadata_fields"]`), and only
five of those are closed sets: `status`, `vertical`, `lead_group`, `program`, `assignee`. The identifier columns
(`lead`, `phone`, `patient_id`, …) grow with every patient — open sets — so they stay in the full-text lane and
are absent here, and `file_url` is not a value anyone types.

Every value offered is the value `prepare_document` really writes, derived from the declarations it reads: the
master behind a column comes off the CRM Lead field itself, and the spelling mirrors `index.py:_read_lead_context`
(the indexed `status` is a stage's `::` LEAF, never the composite PK — a PK would match nothing, silently).

`search.api` calls this behind its own dormant toggle. There is no stopword list: every term is a declared closed-set value, and the spike
measured that a filler list is exactly what destroys a real multi-word term.
"""
import frappe
from frappe.utils.caching import redis_cache

from tatva_connect.search.index import CRMLeadSearch, leaf, normalise, tokens

# A master bigger than this is an OPEN set by definition — it belongs in the full-text lane, not a dictionary.
MASTER_MAX = 5000

# Site-wide, NOT per user: row visibility is already enforced by get_search_filters, so a user naming a value they
# are not entitled to still gets zero rows; a per-user vocabulary would be a second entitlement brain.
_TTL = 600

# A single character matches every query and narrows nothing.
_MIN_TERM_LEN = 2

# Mirrors index.py:_read_lead_context — the CRM Lead field behind each CLOSED metadata column and how the index
# spells its value: `name` = the master's PK, `leaf` = the PK's tail after `::`, `title` = the master's title field.
_SOURCES = (
	("status", "custom_stage", "leaf"),
	("status", "status", "name"),
	("vertical", "custom_vertical", "name"),
	("lead_group", "custom_group", "name"),
	("program", "custom_current_program", "name"),
	("assignee", "lead_owner", "title"),
)


def terms():
	"""Every term the index can filter on -> the meanings it carries, as ((column, value), ...) in a stable order."""
	return _vocabulary().terms


def labels():
	"""Each filterable column -> the label of the CRM Lead field behind it, read from meta and never restated."""
	meta = frappe.get_meta("CRM Lead")
	out = {}
	# First source wins: `status` is declared by custom_stage before status, exactly as _read_lead_context prefers it.
	for column, fieldname, _spelling in _SOURCES:
		field = meta.get_field(fieldname)
		if field and column not in out:
			out[column] = field.label or fieldname
	return out


def match(query):
	"""Split a query into the (column, value) filters the index can apply and the words only full text can."""
	vocab = _vocabulary()
	# index.tokens is the ONE tokeniser: one pair per typed word, normalised for matching and as typed for showing
	# back. Being one pair per word is what removes the old length-mismatch fallback — they cannot fall out of step.
	pairs = tokens(query)
	words = [word for word, _typed in pairs]
	out = frappe._dict(matched=[], ambiguous=[], leftover=[])
	i = 0
	while i < len(words):
		hit = _longest(vocab, words, i)
		if not hit:
			out.leftover.append(pairs[i][1])
			i += 1
			continue
		span, meanings = hit
		if len(meanings) == 1:
			out.matched.append(meanings[0])
		else:
			# A term that means two things is REPORTED, never resolved; its words still reach the full-text lane.
			out.ambiguous.append((" ".join(words[i : i + span]), meanings))
			out.leftover.extend(typed for _word, typed in pairs[i : i + span])
		i += span
	return out


@redis_cache(ttl=_TTL)
def _vocabulary():
	# `span` is the longest declared phrase, so no term the masters declare is ever unreachable by the matcher.
	found = {}
	meta = frappe.get_meta("CRM Lead")
	declared = set(CRMLeadSearch.INDEX_SCHEMA["metadata_fields"])
	for column, fieldname, spelling in _SOURCES:
		if column not in declared:
			continue
		field = meta.get_field(fieldname)
		# The master is read off the Link itself, never named here; an absent field simply contributes nothing.
		if not field or field.fieldtype != "Link" or not frappe.db.table_exists(field.options):
			continue
		for value in _values(field.options, spelling):
			term = normalise(value)
			if len(term) >= _MIN_TERM_LEN:
				found.setdefault(term, set()).add((column, value))
	ordered = {term: tuple(sorted(meanings)) for term, meanings in sorted(found.items())}
	return frappe._dict(terms=ordered, span=max((t.count(" ") for t in ordered), default=0) + 1)


def _values(master, spelling):
	# frappe.get_all is unpermissioned by design: one site-wide vocabulary must not depend on who happened to build it.
	if frappe.db.count(master) > MASTER_MAX:
		return []
	column = frappe.get_meta(master).title_field if spelling == "title" else "name"
	if not column:
		return []
	rows = frappe.get_all(master, fields=[f"`{column}` as value"], order_by="name asc", limit_page_length=0)
	# `index.leaf` itself — a composite PK (`{program}::{stage}`) is indexed as its tail, never whole.
	return [leaf(str(r.value)) if spelling == "leaf" else str(r.value) for r in rows if r.value]


def _longest(vocab, words, i):
	# Longest phrase first: "goodflip care" is a vertical in its own right and must beat the bare "goodflip".
	# It returns how many TYPED words the phrase ate, which is not its word count — "Goodflip-Care" is one word.
	for n in range(min(vocab.span, len(words) - i), 0, -1):
		phrase = " ".join(words[i : i + n])
		if phrase in vocab.terms:
			return n, vocab.terms[phrase]
	return None
