"""WhatsApp inbound follow-up — RETIRED (folded into the automation engine).

The hand-coded "raise ONE reply task on every inbound WhatsApp Message" side-effect used to live here
as `on_inbound_message` (wired on WhatsApp Message · after_insert). It is gone: WhatsApp Message is
now an automation SUBJECT (see `automation.subjects`), so the identical follow-up is expressed as a
user-built rule — On WhatsApp Message Created → Create Task — grain-scoped and dormant until an
operator builds it. No code side-effect remains on inbound messages; the wildcard router carries the
after_insert. Retiring this leaves no orphan behaviour: the rule reproduces the same follow-up.
"""
