"""Fork server for verification runs.

Started once. Pre-imports pytest and the heavy third-party libraries the tests use
(never sqlglot or the tests themselves), then for each job forks a child that runs
pytest in a workspace on the patched tree. Each child imports sqlglot from scratch.

Protocol (one JSON object per line):
  in : {"id": 1, "root": "/tmp/brws0", "argfile": ".../args.txt", "result": ".../r.json",
        "timeout": 30}
  out: {"id": 1, "status": 0, "cpu": 0.42}   (status -1: timed out and killed)
"""
import json
import os
import select
import signal
import sys
import time

# what a test run would import anyway; failures here are harmless
for name in ("pytest", "_pytest.python", "_pytest.unittest", "_pytest.runner", "_pytest.main",
             "pytest_timeout", "unittest", "unittest.mock", "concurrent.futures", "multiprocessing",
             "duckdb", "numpy", "pandas", "pandas.testing", "dateutil.parser", "pytz", "brrun"):
    try:
        __import__(name)
    except Exception:
        pass

for m in list(sys.modules):
    assert not m.startswith("sqlglot") and not m.startswith("tests"), m


def run_child(job):
    os.chdir(job["root"])
    sys.path[:0] = [job["root"]]
    os.environ["BR_RESULT"] = job["result"]
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    import pytest

    try:
        pytest.main(["-q", "-x", "-p", "no:cacheprovider", "-p", "brrun", "-o", "addopts=",
                     "--timeout", "60", "-W", "ignore", "@" + job["argfile"]])
    finally:
        os._exit(0)


def main():
    running = {}  # pid -> (job, deadline)
    buf = b""
    stdin = sys.stdin.fileno()
    open_in = True
    while open_in or running:
        # reap finished children
        for pid in list(running):
            try:
                p, status, ru = os.wait4(pid, os.WNOHANG)
            except ChildProcessError:
                p, status, ru = pid, 0, None
            job, deadline = running[pid]
            if p:
                cpu = (ru.ru_utime + ru.ru_stime) if ru else 0.0
                del running[pid]
                sys.stdout.write(json.dumps({"id": job["id"], "status": status, "cpu": cpu}) + "\n")
                sys.stdout.flush()
            elif time.monotonic() > deadline:
                os.kill(pid, signal.SIGKILL)
                _, _, ru = os.wait4(pid, 0)
                del running[pid]
                sys.stdout.write(json.dumps({"id": job["id"], "status": -1,
                                             "cpu": ru.ru_utime + ru.ru_stime}) + "\n")
                sys.stdout.flush()
        if not open_in:
            time.sleep(0.01)
            continue
        r, _, _ = select.select([stdin], [], [], 0.01)
        if not r:
            continue
        chunk = os.read(stdin, 65536)
        if not chunk:
            open_in = False
            continue
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            if not line.strip():
                continue
            job = json.loads(line)
            pid = os.fork()
            if pid == 0:
                run_child(job)
            running[pid] = (job, time.monotonic() + float(job.get("timeout", 60)))


if __name__ == "__main__":
    main()
