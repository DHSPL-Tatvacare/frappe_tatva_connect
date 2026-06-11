"""Shared display-value normalization for master records (M-2).

Every `field:<name>` master (Doctor, Hospital, City, Group, Program, Vertical,
Task Type, Intake Form, Acefone Account) and every auto-add path (the intake
`_ensure_master`) runs a captured display value through `normalize_display` so
desk, API and form entries converge to ONE canonical row — "Apollo ", "apollo"
and "Apollo" all collapse to "Apollo".

Code/email identifiers (Lead API Field's `field_key`, Lead API Mapping's
`partner_user`) are deliberately NOT normalized — they are stable machine keys,
not human display text.
"""
import re
import unicodedata


def normalize_display(value):
	"""Canonicalize a human-typed display string (Unicode-safe).

	* trim leading/trailing whitespace
	* collapse internal runs of whitespace to a single space
	* strip stray trailing punctuation (.,;: and similar) that a typist leaves
	* title-case for a consistent display form

	Returns the cleaned string, or the original value unchanged when it isn't a
	str (so a None/blank stays None/blank for the caller to handle).
	"""
	if not isinstance(value, str):
		return value
	# NFKC folds compatibility forms (full-width, ligatures) to a canonical shape.
	s = unicodedata.normalize("NFKC", value)
	s = s.strip()
	if not s:
		return ""
	# collapse internal whitespace (incl. tabs/newlines) to single spaces
	s = re.sub(r"\s+", " ", s)
	# strip stray trailing punctuation a typist leaves (keep letters/digits/closing brackets)
	s = re.sub(r"[\s.,;:!\-/\\|]+$", "", s)
	# strip the symmetric stray leading punctuation
	s = re.sub(r"^[\s.,;:!/\\|]+", "", s)
	s = s.strip()
	if not s:
		return ""
	# Title-case for display. str.title() mishandles apostrophes/acronyms but is the
	# agreed display form here; keep it simple per the plan (no clever casing rules).
	return s.title()


def normalize_field(doc, fieldname):
	"""validate-hook helper: normalize one Data field in place if it has a value."""
	val = doc.get(fieldname)
	if val:
		doc.set(fieldname, normalize_display(val))
