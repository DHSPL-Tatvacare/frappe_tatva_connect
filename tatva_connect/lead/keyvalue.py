"""The ONE identity rule for a key-value lead section: a question is identified by a digest of itself.

A key-value section (`CRM Lead Section.is_key_value`) holds externally authored answers as one row per
question. The question is kept exactly as it was asked and is mapped to nothing, so its own text is the
identity, and `identity_of` is the single place that text becomes a key.

A digest rather than the text itself, for one reason: a question key is unbounded. The live forms reach
292 characters and nothing stops a longer one. Indexing text needs a prefix length, and any prefix is a
number a future question can exceed, at which point two different questions silently share an index
entry. A digest is fixed width whatever it is given, so the index is a constant size forever. The same
question therefore hashes alike across every form, which is what makes one question one thing without
anybody declaring that it is. `api/_base.py` and `workflow_engine/versions.py` key rows the same way.

WHERE each part of a row lives is not here: `CRM Lead Section` names the identity, answer, label and
question columns, and every reader asks it. This module holds the three RULES that are nobody else's —
how an identity is derived, which answer is the current one, and how several selections become one answer.
"""
import hashlib

from frappe.utils import cint, cstr

# A checkbox question answers with several values and every one of them is the record; the joined string is what a person reading the answer would write down.
ANSWER_JOIN = ", "
# The one column of a key-value row the SECTION does not name, because no caller authors it: the server stamps where the answer came from. Named here so the writer and the history reader share one name.
ORIGIN_FIELD = "origin"


def answer_of(value):
	"""Several selections as ONE answer. A scalar is its own answer, so a caller that already joined its
	own multi-select is unchanged; a list is joined in the order it was sent, because the order is the
	form's option order and sorting it would make a re-send look like a changed answer."""
	if isinstance(value, (list, tuple)):
		return ANSWER_JOIN.join(cstr(v) for v in value)
	return value


def identity_of(question: str) -> str:
	"""The stable identity of a question key. Fixed width for any input, so no index depends on a length."""
	return hashlib.sha256((question or "").encode()).hexdigest()  # not SQL — a deterministic row identity


def newest_first(rows):
	"""A question's answers, most recent first. THE order for a key-value section, so the Data tab, the
	history and anything later all agree on which answer is current.

	Ordered by `idx`, the child table's own sequence and the doctype's declared sort field. Not by
	`creation`: answers written in one save share a timestamp to the second, and the `(creation, name)`
	tiebreak a dated section uses falls back to a random child hash, which is no order at all."""
	return sorted(rows, key=lambda r: cint(r.get("idx")), reverse=True)
