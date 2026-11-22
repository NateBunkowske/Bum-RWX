"""pytest plugin used at decision time: run the selected tests on the patched tree,
with the optimizer/executor process pools replaced by an in-process pool (args and
results still pickled, as a real pool would), and record every test's outcome.

A test counts as failed if any of its reports (setup / call / teardown / subtest)
failed, or if its file failed to collect. Results go to $BR_RESULT as JSON:
{"failed": [nodeid...], "seen": [nodeid...], "collect_failed": [path...]}.
"""
import json
import os
import pickle
import sys

import pytest

failed = set()
seen = set()
collect_failed = set()
durations = {}
state = {"sqlglot": None}


def _rt(obj):
    return pickle.loads(pickle.dumps(obj))


class _SyncPool:
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        if initializer:
            initializer(*_rt(initargs))

    def submit(self, fn, *a, **k):
        from concurrent.futures import Future

        f = Future()
        try:
            a, k = _rt((a, k))
            f.set_result(_rt(fn(*a, **k)))
        except BaseException as e:  # noqa
            f.set_exception(e)
        return f

    def map(self, fn, *its, **kw):
        return [_rt(fn(*_rt(args))) for args in zip(*its)]

    def shutdown(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def pytest_collection_finish(session):
    from concurrent.futures import ProcessPoolExecutor

    mod = sys.modules.get("sqlglot")
    state["sqlglot"] = getattr(mod, "__file__", None)

    for name, mod in list(sys.modules.items()):
        if name.startswith("tests") and getattr(mod, "ProcessPoolExecutor", None) is ProcessPoolExecutor:
            mod.ProcessPoolExecutor = _SyncPool


def pytest_collectreport(report):
    if report.failed:
        collect_failed.add(report.nodeid)


def pytest_runtest_logreport(report):
    seen.add(report.nodeid)
    if report.failed:
        failed.add(report.nodeid)
    durations[report.nodeid] = durations.get(report.nodeid, 0.0) + (report.duration or 0.0)


def pytest_unconfigure(config):
    out = os.environ.get("BR_RESULT")
    if out:
        with open(out, "w") as f:
            json.dump({"failed": sorted(failed), "seen": sorted(seen),
                       "collect_failed": sorted(collect_failed), "durations": durations,
                       "sqlglot": state["sqlglot"]}, f)
