# Memory-first FAST backtests

FAST now loads one existing 31-day evaluation window plus its required warmup,
then releases its numeric candles, timeframe arrays, swings and facts before
loading the next. Gold 831/832 use seven warmup days; the maximum supported
EMA200/4h configuration uses 54. The result therefore never retains five years
of candles. Monthly source JSON is downloaded sequentially without the shared
RAM or disk history caches. HTTP responses and source file handles close after
each read. Source files remain commit-pinned and capped at 16 MiB each.

The 31-day evaluation boundaries are intentional compatibility behavior. Each
window recomputes facts from the same legacy warmup boundary; downloading a
new calendar month does not reset indicators. Existing serialized continuation
carries pending setups, open trades, partial TP1/protected stops, evaluator
status, ordinal and balance. Only the final evaluation window finalizes the
remaining open trade. An empty weekend-only final window remains valid when
the overall request contains evaluation candles.

Window result dictionaries are written to a private job-local `chunks-*`
directory. Aggregation rereads one chunk at a time in original order, preserving
binary64 arithmetic and diagnostic insertion order. The final result is still
a Python object, but source window objects are not retained alongside it.
`json.dump` writes progressively; completed HTTP polling streams the existing
JSON envelope in 64 KiB blocks rather than decoding/copying the entire result
in the API process. Linux advisory file-cache eviction follows large temporary
file writes/reads. Correctness does not depend on eviction succeeding.

Scratch files are removed on normal completion and exceptions. The owning
manager removes abandoned scratch and incomplete results after reaping a
cancelled, timed-out or failed worker, and on restart. Completed result files
retain the existing one-hour/20-job retention policy so polling still works;
they are outputs, not working scratch. Nothing is stored in Neon.

The shared OS heavy-work lease remains authoritative across FAST and legacy
replay/simulator endpoints. Only one heavy calculation may run. The manager
waits for and reaps its subprocess before releasing ownership. The timeout is
30 minutes to accommodate low-memory work without adding computation sleeps.

## Validation

New tests independently compare bounded preparation with global aggregation
and noncompact legacy facts across month/year boundaries, nonaligned endpoints,
missing/off-grid bars and the longest EMA warmup. Additional tests cover pending
confirmation/remembered BOS, TP1/protected-stop continuation, empty weekend tails,
frame release, ownership and streamed response eviction, and scratch cleanup.

Capacity testing uses a disposable local Linux VM, full API plus child in one
512 MiB cgroup with swap disabled, and an external observer/load driver. It does
not deploy to Render. Python 3.14.3 and eight production library versions match;
Linux ARM64 differs from Render x86_64. Production capacity estimates add the
measured total-cgroup increment to the previously measured production baseline,
not isolated child RSS. See the task output report for final measurements and
explicit limitations. No staging harness or credentials belong in this branch.
