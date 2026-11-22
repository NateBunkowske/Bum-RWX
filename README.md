# Blast Radius — coverage-driven test selection for sqlglot

For each patch against sqlglot `eb11aec4`, the selector works out a sound static
selection from committed maps, then, where it pays, **runs** part of that selection on
the patched tree and lists only what can still fail.

## How a change maps to tests

All maps are built once from the pinned commit (`tools/build.sh`) and committed:

| Map | How it is built |
| --- | --- |
| Per-test line + function-entry coverage of `sqlglot/` and `tests/` | `tools/mapplugin.py`: each test runs in its own `fork()` of the post-collection pytest process, so caches (`_DISPATCH_CACHE`), lazy dialect imports and `setUpClass` are charged to every test that depends on them. The optimizer/executor tests' spawn pools are swapped for an in-process pool that still pickles arguments and results, so worker code (and `serde`) is traced. |
| Files each test opens | `open` / `io.open` hook (fixtures). |
| sqlglot classes each test uses | MRO of every instantiated `Expr`, `Generator`, `Parser`, `Tokenizer`, `Dialect`, plus dialect lookups. |
| Import-time lines | `tools/importmap.py`: which function bodies run while a module is imported, and which module's import ran them. |
| Static references and importers | Which functions / class bodies mention each name; which modules `from X import name`. |

At run time each patch is applied in memory to the pinned source and both versions are parsed:

* ASTs equal (comments, whitespace) → nothing from that file.
* Edited statement in a function body → tests that executed that statement.
  Lines inserted into a body → tests that entered the function.
* Lambda bodies in class-level tables → tests that called that lambda.
* Class-body edits → tests that used the class or a subclass, plus anything that
  ran its methods or imports it by name. Untracked classes fall back to every test
  touching the module or referencing the name.
* Module-level bindings → tests touching the module, plus users of the bound names
  (references and `from … import name`).
* Code that runs at import time → tests touching the importing modules.
* Fixture / data files → tests that opened them.
* Syntax errors, unappliable hunks, renames, deletions, config files → every test.
* **Import probe.** Any edit that runs at import time (class bodies, module level,
  def headers, functions run during import) is checked by importing every sqlglot
  module and every dialect in a subprocess, with the patched files served from
  memory. If anything raises or hangs, every test runs: a module that fails to
  import breaks collection of every test file that imports it (about 0.5 CPU s each).

## Verification (running tests at decision time)

Listing a test costs its recorded seconds on the busiest worker plus 2 s per worker;
running it here costs our CPU seconds. Per commit the selector picks the cheapest of:

* **list** the static selection;
* **run every light test** (recorded and in-process time under 5 s) on the patched
  tree, then list only the ones that failed or never ran (plus the heavy ones);
* **run the heavy tests' witnesses only** — the 25 cheapest selected tests from the
  same file as each heavy test — and list the rest.

Tests that passed are removed; anything that failed, did not run (`-x` stops at the
first failure), crashed or ran far over its recorded time stays listed.

**The one bet:** a heavy test that is expensive to list (`test_execute_tpcds`,
`test_max_nodes`, `test_generator_memory`; recorded ≥ 5 s) is dropped when the run
found no failure and all of its witnesses passed. On 137 mutants with full ground
truth, every heavy-test failure came with failures in 16–18 of its 25 witnesses, and
the bet never missed. It is never taken for commits that touch fixtures or other
data files. `BR_DROP_HEAVY=0` turns it off.

Runs happen in two `/tmp` workspaces (sources copied, fixtures symlinked), in suite
order, with the process pools of the optimizer/executor tests replaced by an
in-process pool that still pickles (`brrun.py`). A fork server (`brserver.py`)
pre-imports pytest, duckdb and pandas — never sqlglot. A canary (a planted import
error) must be caught before any verification is trusted, and each run checks it
imported the patched sqlglot. A deadline keeps the batch well inside the time limit.

Workers: longest-processing-time packing for every worker count 1–8, keeping the
cheapest `max load + 2 s × workers` (whole milliseconds, as the scorer counts).

## Checking for misses

`tools/mutgen.py` makes sample-like mutants; `tools/harness.py` runs every test the
selector did **not** pick on each mutant. Any failure there is a miss.
