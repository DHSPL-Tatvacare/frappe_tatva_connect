# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""authz — the permission / access / VAPT test framework.

Read TESTS.md (same folder) for the philosophy and map. Spine modules:
  comms.py    — the mandatory COMMS-OFF safety interlock (every test calls it first)
  grains.py   — the 5 canonical test grains + master-existence validation
  roster.py   — the always-on dummy-user roster (real roles only)
  oracle.py   — native_would_allow(): Frappe's own engine is the judge
  base.py     — AuthzTestCase: comms gate + rollback wiring + reusable helpers
"""
