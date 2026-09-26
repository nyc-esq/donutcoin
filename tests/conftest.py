"""Environment the whole suite needs, set once, before any test module imports the chain.

Every test file used to set DONUTCOIN_EASY_POW itself, and exactly one of them set
DONUTCOIN_NOTE_RULES_HEIGHT. That made the suite order-dependent:
test_note_submission_needs_a_name_and_hide_list passed when test_consensus.py had been imported
first and failed when run alone, because without the note rules active the node never reaches the
named-author check. A test whose result depends on what ran before it will eventually pass while
the code is broken, which is worse than having no test. (2026-09-19)

These are read at import time by donutcoin.chain, so they must be set before pytest collects any
module. conftest.py is imported first, which is the only place that is guaranteed.
"""
import os

os.environ["DONUTCOIN_EASY_POW"] = "1"              # tiny proof of work, so tests mine in milliseconds
os.environ["DONUTCOIN_NOTE_RULES_HEIGHT"] = "0"     # note rules apply from genesis
# Coinbase maturity stays at the pre-fork 3 for the suite: the tests were written against it
# and spend rewards a few blocks old. Pinned far out rather than left to the production
# default of 3,444, which only works because test chains never get that tall. The tests that
# care about the 21-block rule monkeypatch this themselves.
os.environ["DONUTCOIN_MATURITY_RULES_HEIGHT"] = "1000000"
