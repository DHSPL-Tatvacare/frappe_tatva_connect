# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The registry — the curated test surface as DATA (TESTS.md §6).

  dimensions.py  the tuple dimensions + values (for coverage auditing)
  attacks.py     the 13 attack vectors (the directions of breakage)
  cases.py       CaseSpec + the curated case list; each case = (attack × principal × target ×
                 surface × action) with a stable id, an English description, an expected verdict,
                 and the oracle to judge it against.

Add a case = add a row in cases.py. Stable ids make every case individually traceable in the
report and the confusion matrix.
"""
