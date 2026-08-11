"""Derived fields declared in CODE. There are none, and that is the point.

A derived field is OPERATOR DATA: a `CRM Derived Field` row authored in Desk, proven by `verify()` at
Save and live the moment it is enabled — no deploy, no migrate, no restart. Task Status (`due_state` on
CRM Task) was the first citizen and moved here-to-there on 2026-07-31; it ships as a row, from
`go-live/3-seed/db-seeds/1-before-load/2026-07-31-6b7cb13-derived-fields.sql`.

This module stays because the registry takes two sources and the SECOND one is still legitimate: a
declaration that must exist before any database row can (a bootstrap), or one an operator must not be
able to retire. Register it here exactly as a row declares itself — `derived.register(DerivedField(...))`
— and it wins the merge, because `CRM Derived Field.validate` refuses a row that would shadow it.

Whichever source a declaration comes from, it lands in the ONE registry and nothing downstream — the
engine, the five menus, the renderers, the wire contract — ever learns which one it was.

Plan: docs/plans/tasks-ui/2026-07-31-derived-field-head.md
"""
