"""tatva_connect automations — the single home for event-driven side-effects.

Providers (WATI, Acefone, REST writers) only persist their own records. Every
side-effect hangs off a Frappe ``doc_events`` hook wired in ``hooks.py`` to a
thin handler in this package:

  leads.py     CRM Lead.before_insert    -> dedup guard (block dup mobile_no)
  tasks.py     CRM Task.validate         -> seed checklist from template, then
                                            enforce it on Done (ordered handlers)
               create_followup_task()    -> idempotent task helper (also an API)
  whatsapp.py  WhatsApp Message.after_insert -> inbound -> follow-up task
  registry.py  list_automations()        -> read-only inventory of what fires

Keep each handler small and provider-agnostic. No rules engine, no speculative
abstraction — one function per event.
"""
