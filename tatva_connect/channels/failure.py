# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""How a failure is WORDED, expressed once — the policy, not the place it is kept.

THREE SURFACES RECORD A FAILURE AND THEY CANNOT SHARE A COLUMN. A send the provider refused is a row
this app writes (`automation.sends`); a failure the provider reports later ticks a row that already
exists (`whatsapp.ingest`); a manual send refused in front of a rep is a message on their screen
(`whatsapp.message`). Where each is stored genuinely differs, and forcing one column would buy symmetry
by making all three harder to read.

What they must never disagree on is the WORDING. Written three times it is one rule in three places,
and the day they drift nobody finds out — they had already drifted: the same provider refusal read
`131026 · Message undeliverable` when it arrived as a status and `Message undeliverable` when it was
caught at send, so a report grouping by reason split one failure into two buckets and neither could be
reconciled with the other.

WHAT IS DELIBERATELY NOT HERE: which column the sentence lands in, and whether a row survives at all.
Those differ per surface and stay with the surface that owns them.

A code is included WHEN THE PROVIDER GAVE ONE and never invented: a status event carries `failedCode`,
while no send envelope this app has observed carries a code at all. So the same rule renders both — the
pair when there is a pair, the sentence alone when that is all there is.
"""

# A readability cap, not a column limit — these land in Small Text, which would take far more. Telephony
# already chose 500 for `recording_error`; a reason is for a human to read and act on, and a provider
# that answers with a page of JSON is still answering one question.
MAX_LENGTH = 500

# Between a code and its sentence. Distinctive on purpose: a reason may itself contain a comma or a dash.
SEPARATOR = " · "


def reason(code=None, detail=None, fallback=None) -> str | None:
	"""One failure, worded one way. Returns None only when nothing at all was given.

	`fallback` is the caller's own sentence for a provider that said nothing useful — it is used only
	when neither half arrived, so a provider's own words always win over ours.
	"""
	parts = [str(part).strip() for part in (code, detail) if part not in (None, "")]
	text = SEPARATOR.join(part for part in parts if part) or (str(fallback).strip() if fallback else "")
	return text[:MAX_LENGTH] or None
