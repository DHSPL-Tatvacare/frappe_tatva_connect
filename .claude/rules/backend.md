---
paths:
  - "tatva_connect/**/*.py"
  - "tatva_connect/**/*.json"
---

# Backend: before you write a line

The rules are in `CLAUDE.md` (A.1, A.8, A.12, A.18, A.19, S.1, S.2) and `docs/conventions/backend/`. This file is the checklist that applies them.

1. Search before you write. Name what you searched in your answer.
   - Frappe first: `frappe.db.*`, `get_meta`, `has_permission`, `enqueue`, `cache`, `assign_to`, `share`, `safe_eval`.
   - Then ours: `tatva_connect/utils.py` (rate limits, drains, lanes, deadlock retry, masking, safe URLs), `phone.py`, `tabular.py` (CSV/XLSX), `propagate.py` (`fail_safe`), `routing.py`, `access/`, `storage/`, and the module's own package.
   - `grep -rn "def <verb>" tatva_connect/` for the verb you need. A negative grep is not proof; widen it.
2. Reuse the shared function. If two paths need the same logic, both call one function. Never copy a field list the schema already declares.
3. No hardcoded names, grains, roles or limits. Read them from meta, a Settings doctype or the registry.
4. Comments are one line. Docstrings stay short.
5. Every `except` logs, surfaces or justifies itself in one line. Never swallow `ValidationError` or `PermissionError`.
6. Every new switch defaults OFF. Every structural patch also sits in `schema_setup._STEPS`.
7. Prove it on the real entry point: one or two tests through `bench run-tests --module ...`, and say they fail on the old code.
