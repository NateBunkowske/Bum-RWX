"""Blast Radius selector.

For each patch against the pinned sqlglot commit, pick the tests that could fail
and split them over workers.

How a change maps to tests (all maps are built from the pinned commit, never from
patches):
  * a per-test coverage map (each test run in a fresh fork after collection, so
    caches, lazy imports and setUpClass are charged to every test that needs them),
  * which files each test opens (fixtures),
  * which sqlglot classes each test instantiates (MRO), and
  * which lines run at import time and which module's import ran them.

Per changed file:
  * comment / whitespace only (ASTs equal)          -> nothing from code
  * edited statement inside a function body         -> tests that executed it
  * lines inserted into a function body             -> tests that entered it
  * class body / module level / def header changes  -> tests that touched the module
                                                       (or modules importing from it)
  * code that runs at import time                   -> tests touching the importers
  * fixtures and other data files                   -> tests that opened them
  * anything unparseable or unexpected              -> every test
"""

import ast
import json
import lzma
import os
import pickle
import re
import sys
from pathlib import Path

DATA = Path(os.environ.get("BR_DATA", "/data"))
HERE = Path(__file__).resolve().parent
SRC = Path(os.environ.get("BR_SRC", "/opt/sqlglot"))
MAP = Path(os.environ.get("BR_MAP", HERE / "map.pkl.xz"))
WORKER_MS = 2000
MAX_WORKERS = 8


def log(*a):
    print(*a, file=sys.stderr)


# --------------------------------------------------------------------------- patch


class FilePatch:
    __slots__ = ("old", "new", "hunks", "binary")

    def __init__(self):
        self.old = self.new = None
        self.hunks = []  # list of (old_start, [(tag, text)])
        self.binary = False


HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_patch(text):
    files = []
    cur = None
    lines = text.split("\n")
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if line.startswith("diff --git "):
            cur = FilePatch()
            files.append(cur)
            i += 1
            continue
        if line.startswith("--- ") and i + 1 < n and lines[i + 1].startswith("+++ "):
            if cur is None or cur.hunks:
                cur = FilePatch()
                files.append(cur)
            cur.old = _path(line[4:])
            cur.new = _path(lines[i + 1][4:])
            i += 2
            continue
        if line.startswith("Binary files") or line.startswith("GIT binary patch"):
            if cur is not None:
                cur.binary = True
            i += 1
            continue
        m = HUNK.match(line)
        if m and cur is not None:
            old_start = int(m.group(1))
            old_len = int(m.group(2)) if m.group(2) is not None else 1
            new_len = int(m.group(4)) if m.group(4) is not None else 1
            body = []
            i += 1
            o = nw = 0
            while i < n and (o < old_len or nw < new_len):
                l = lines[i]
                if l.startswith("\\"):
                    i += 1
                    continue
                tag = l[:1] if l else " "
                if tag == " ":
                    o += 1
                    nw += 1
                elif tag == "-":
                    o += 1
                elif tag == "+":
                    nw += 1
                else:
                    raise ValueError("bad hunk line")
                body.append((tag, l[1:]))
                i += 1
            while i < n and lines[i].startswith("\\"):
                i += 1
            cur.hunks.append((old_start, old_len, body))
            continue
        if cur is not None and (line.startswith("rename ") or line.startswith("copy ")):
            cur.binary = True  # treat renames/copies as unknown
        i += 1
    return files


def _path(p):
    p = p.split("\t")[0].strip()
    if p == "/dev/null":
        return None
    if p.startswith("a/") or p.startswith("b/"):
        p = p[2:]
    return p


def apply_hunks(old_text, hunks):
    """Return new text plus changed line numbers: removed old lines, added new lines,
    and for each '+' line whether it is paired 1:1 with a '-' line."""
    old = old_text.split("\n")
    out = []
    pos = 0  # index into old (0-based)
    removed = []
    added = []
    paired_new = {}  # new line -> old line, for equal-size -/+ runs
    for old_start, old_len, body in hunks:
        # "-10,0" means: insert after old line 10
        start = old_start if old_len == 0 else old_start - 1
        if start < 0:
            start = 0
        if start < pos:
            raise ValueError("overlapping hunks")
        out.extend(old[pos:start])
        pos = start
        j = 0
        while j < len(body):
            tag, txt = body[j]
            if tag == " ":
                if pos >= len(old) or old[pos] != txt:
                    raise ValueError("context mismatch")
                out.append(txt)
                pos += 1
                j += 1
                continue
            minus = []
            plus = []
            while j < len(body) and body[j][0] == "-":
                minus.append(body[j][1])
                j += 1
            while j < len(body) and body[j][0] == "+":
                plus.append(body[j][1])
                j += 1
            olds = []
            for k, txt2 in enumerate(minus):
                if pos >= len(old) or old[pos] != txt2:
                    raise ValueError("removed line mismatch")
                removed.append(pos + 1)
                olds.append(pos + 1)
                pos += 1
            for k, txt2 in enumerate(plus):
                out.append(txt2)
                ln = len(out)
                added.append(ln)
                if len(plus) == len(minus):
                    paired_new[ln] = olds[k]
    out.extend(old[pos:])
    return "\n".join(out), removed, added, paired_new


# --------------------------------------------------------------------------- AST helpers

FUNC = (ast.FunctionDef, ast.AsyncFunctionDef)
COMPOUND = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try,
            ast.Match, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
if hasattr(ast, "TryStar"):
    COMPOUND = COMPOUND + (ast.TryStar,)


def first_line(node):
    decos = getattr(node, "decorator_list", None)
    if decos:
        return min(d.lineno for d in decos)
    return node.lineno


class Locus:
    """Where a line sits: chain of enclosing def/class nodes and the innermost stmt."""

    __slots__ = ("path", "func", "in_body", "stmts", "lambdas")

    def __init__(self):
        self.path = []  # list of ("class"|"def", name)
        self.func = None  # innermost FunctionDef whose body holds the line
        self.in_body = False
        self.stmts = []  # stmt chain inside the innermost function body / scope
        self.lambdas = []


def locate(tree, line):
    loc = Locus()
    node = tree
    scope_stmts = []
    while True:
        body_lists = []
        for field in ("body", "orelse", "finalbody", "handlers", "cases"):
            v = getattr(node, field, None)
            if isinstance(v, list):
                body_lists.append(v)
        nxt = None
        for lst in body_lists:
            for ch in lst:
                if isinstance(ch, ast.stmt):
                    lo, hi = first_line(ch), ch.end_lineno
                elif hasattr(ch, "lineno"):
                    lo, hi = ch.lineno, ch.end_lineno
                else:  # match_case
                    lo = ch.pattern.lineno
                    hi = ch.body[-1].end_lineno if ch.body else lo
                if lo <= line <= hi:
                    nxt = ch
                    break
            if nxt is not None:
                break
        if nxt is None:
            break
        if isinstance(nxt, FUNC):
            loc.path.append(("def", nxt.name))
            body_lo = nxt.body[0].lineno if nxt.body else nxt.end_lineno
            if body_lo <= line:
                loc.func = nxt
                loc.in_body = True
                scope_stmts = []
            else:
                loc.func = nxt
                loc.in_body = False
                return loc
        elif isinstance(nxt, ast.ClassDef):
            loc.path.append(("class", nxt.name))
            body_lo = nxt.body[0].lineno if nxt.body else nxt.end_lineno
            loc.func = None
            loc.in_body = False
            scope_stmts = []
            if line < body_lo:
                loc.stmts = [nxt]
                return loc
        elif isinstance(nxt, ast.stmt):
            scope_stmts.append(nxt)
        node = nxt
    loc.stmts = scope_stmts
    return loc


def index_defs(tree):
    """path tuple -> node for every def/class."""
    out = {}

    def walk(node, path):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, FUNC):
                p = path + (("def", ch.name),)
                out.setdefault(p, ch)
                walk(ch, p)
            elif isinstance(ch, ast.ClassDef):
                p = path + (("class", ch.name),)
                out.setdefault(p, ch)
                walk(ch, p)
            elif isinstance(ch, (ast.stmt, ast.excepthandler, ast.match_case)):
                walk(ch, path)

    walk(tree, ())
    return out


INVISIBLE = (ast.Pass, ast.Global, ast.Nonlocal)


def is_invisible(stmt):
    if isinstance(stmt, INVISIBLE):
        return True
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
        return True
    return False


def changed_cols(old_line, new_line, new_side=False):
    """Column range [c0, c1) of the edit, on the old line (or the new line)."""
    a, b = old_line, new_line
    p = 0
    while p < len(a) and p < len(b) and a[p] == b[p]:
        p += 1
    q = 0
    while q < len(a) - p and q < len(b) - p and a[-1 - q] == b[-1 - q]:
        q += 1
    if new_side:
        return p, len(b) - q
    return p, len(a) - q


def _within(first, last, ln, c0, c1, loose=False):
    """Is columns c0..c1 of line ln inside the source span first..last?"""
    sl, sc = first.lineno, first.col_offset
    el, ec = last.end_lineno, last.end_col_offset
    if loose:  # any overlap
        if ln < sl or ln > el:
            return False
        lo = sc if ln == sl else 0
        hi = ec if ln == el else 10**9
        return c0 < hi and c1 > lo or (c0 == c1 and lo <= c0 <= hi)
    if (ln, c0) < (sl, sc):
        return False
    if (ln, c1) > (el, ec):
        return False
    return True


NARROW_ATTRS = os.environ.get("BR_NARROW_ATTRS", "1") == "1"


CONST_NAME = re.compile(r"^_?[A-Z][A-Z0-9_]*$")


def attr_target(stmt):
    """Name of a plain `NAME = value` / `NAME: T = value` class-body assignment of an
    UPPER_CASE constant. Lower-case names (e.g. `foo_sql = rename_func(...)`) can be
    reached by dynamic dispatch (getattr(self, f"{key}_sql"), dir(cls)), so no."""
    name = None
    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
        name = stmt.targets[0].id
    elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value is not None:
        name = stmt.target.id
    if name is None or not CONST_NAME.match(name):
        return None
    return name


def bound_names(stmt):
    """Names a module-level statement binds, and whether it is a plain binding."""
    names = set()
    simple = True
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        names.add(stmt.name)
    elif isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
        for t in targets:
            for n in ast.walk(t):
                if isinstance(n, ast.Name):
                    names.add(n.id)
        if any(not isinstance(t, ast.Name) for t in targets):
            simple = False  # X[k] = ..., X.a = ...: mutates something
    elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
        for a in stmt.names:
            names.add((a.asname or a.name).split(".")[0])
    else:
        simple = False
        for n in ast.walk(stmt):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(n.name)
            elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                names.add(n.id)
    return names, simple


# --------------------------------------------------------------------------- import probe

PROBE = r"""
import importlib, importlib.abc, importlib.util, json, os, pkgutil, sys
src, patched = json.loads(sys.stdin.read())
sys.path.insert(0, src)
paths = {os.path.join(src, rel): text for rel, text in patched.items()}

class Loader(importlib.abc.SourceLoader):
    def __init__(self, path):
        self.path = path
    def get_filename(self, fullname):
        return self.path
    def get_data(self, path):
        if path in paths:
            return paths[path].encode("utf-8")
        with open(path, "rb") as f:
            return f.read()

class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        base = os.path.join(src, *name.split("."))
        for cand, pkg in ((base + ".py", False), (os.path.join(base, "__init__.py"), True)):
            if cand in paths:
                return importlib.util.spec_from_file_location(
                    name, cand, loader=Loader(cand),
                    submodule_search_locations=[os.path.dirname(cand)] if pkg else None)
        return None

sys.meta_path.insert(0, Finder())
import sqlglot
for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
    if info.name != "sqlglot.__main__":
        importlib.import_module(info.name)
from sqlglot.dialects.dialect import Dialect
Dialect.classes
print("ok")
"""


def import_probe(patched):
    """Does sqlglot (every module, every dialect) still import with these files?"""
    import subprocess

    try:
        r = subprocess.run(
            [sys.executable, "-I", "-S", "-c", PROBE],
            input=json.dumps([str(SRC), patched]),
            capture_output=True, text=True, timeout=30,
        )
    except Exception as e:  # timeout: an import that hangs is broken too
        log("probe:", e)
        return False
    if r.returncode != 0 or r.stdout.strip() != "ok":
        log("probe failed:", (r.stderr.strip().splitlines() or ["?"])[-1][:200])
        return False
    return True


_PROBE_BASELINE = []


def probe_works():
    if not _PROBE_BASELINE:
        _PROBE_BASELINE.append(import_probe({}))
    return _PROBE_BASELINE[0]


# --------------------------------------------------------------------------- map


class Map:
    def __init__(self, path, tests):
        with open(path, "rb") as f:
            m = pickle.loads(lzma.decompress(f.read()))
        self.sets = m["sets"]
        self.lines = m["lines"]
        self.files = m["files"]
        self.types = m["types"]
        self.imports = m["imports"]
        self.collect = m["collect"]
        self.import_opened = m.get("import_opened", {})
        self.static_importers = m.get("static_importers", {})
        self.refs = m.get("refs", {})
        self.name_importers = m.get("name_importers", {})
        self._refs_cache = {}
        self.lazy_bits = 0
        self._expr = None
        self.expr_names = set(m.get("expr_names", ()))
        ids = m["ids"]
        num = {t["id"]: t["number"] for t in tests}
        self.num_of_bit = [num.get(i) for i in ids]
        self.all_bits = (1 << len(ids)) - 1
        known = set(ids)
        # tests we have no coverage for are always run
        self.unmapped = [t["number"] for t in tests if t["id"] not in known]
        self.file_bits = {}
        for b, i in enumerate(ids):
            f = i.split("::", 1)[0]
            self.file_bits[f] = self.file_bits.get(f, 0) | (1 << b)
        self._touch = {}
        self._types_by_mod = None

    def _expr_index(self):
        if self._expr is None:
            idx = {}
            for k, sid in self.types.items():
                mod, qual = k.split(":", 1)
                if mod.startswith("sqlglot.expressions") and "." not in qual:
                    idx[qual] = idx.get(qual, 0) | self.sets[sid]
            self._expr = idx
        return self._expr

    def is_expr_class(self, name):
        return name in self.expr_names

    def expr_bits(self, name):
        return self._expr_index().get(name, 0)

    def line(self, rel, ln):
        m = self.lines.get(rel)
        if not m:
            return 0
        s = m.get(ln)
        return self.sets[s] if s is not None else 0

    def span(self, rel, lo, hi):
        m = self.lines.get(rel)
        if not m:
            return 0
        bits = 0
        sets = self.sets
        for ln in range(lo, hi + 1):
            s = m.get(ln)
            if s is not None:
                bits |= sets[s]
        return bits

    def entry(self, rel, node):
        return self.line(rel, -first_line(node)) | self.line(rel, -node.lineno)

    def touch(self, rel):
        """Tests that executed anything in the module or used a class defined in it."""
        if rel in self._touch:
            return self._touch[rel]
        bits = 0
        m = self.lines.get(rel)
        if m:
            for s in set(m.values()):
                bits |= self.sets[s]
        mod = module_name(rel)
        if mod:
            if self._types_by_mod is None:
                tb = {}
                for k, s in self.types.items():
                    md = k.split(":", 1)[0]
                    tb[md] = tb.get(md, 0) | self.sets[s]
                self._types_by_mod = tb
            bits |= self._types_by_mod.get(mod, 0)
        bits |= self.file_bits.get(rel, 0)
        self._touch[rel] = bits
        return bits

    def opened(self, rel):
        s = self.files.get(rel)
        return self.sets[s] if s is not None else 0


def module_name(rel):
    if not rel.endswith(".py"):
        return None
    p = rel[:-3].replace("/", ".")
    if p.endswith(".__init__"):
        p = p[: -len(".__init__")]
    return p


# --------------------------------------------------------------------------- analysis

ALL = -1  # sentinel
ALL_BITS_SENTINEL = 1 << 100000


class Analyzer:
    def __init__(self, mp):
        self.mp = mp
        self.src_cache = {}

    def old_text(self, rel):
        if rel not in self.src_cache:
            p = SRC / rel
            try:
                self.src_cache[rel] = p.read_text(encoding="utf-8")
            except (FileNotFoundError, IsADirectoryError, UnicodeDecodeError):
                self.src_cache[rel] = None
        return self.src_cache[rel]

    def commit(self, text):
        try:
            fps = parse_patch(text)
        except Exception as e:
            log("unparseable patch:", e)
            return ALL, "unparseable"
        if not fps:
            return 0, "empty"
        bits = 0
        why = []
        self.patched = {}  # rel -> new text, for sqlglot/*.py files
        self.all_new = {}  # rel -> new text, every .py file we applied
        self.verifiable = True
        self.import_level = False
        for fp in fps:
            b, w = self.file(fp)
            why.append(w)
            if b == ALL:
                return ALL, ";".join(why)
            bits |= b
        if self.import_level and self.patched:
            # an edit that runs at import time could make a module fail to import,
            # which fails every test whose file (or whose run) imports it
            if not import_probe(self.patched):
                if probe_works():
                    return ALL, ";".join(why) + ";import-fails"
                log("probe unavailable; keeping selection")
            why.append("import-ok")
        return bits, ";".join(why)

    def file(self, fp):
        rel = fp.old or fp.new
        if fp.binary or fp.old is None or fp.new is None or fp.old != fp.new or not (
            rel.endswith(".py") and (rel.startswith("sqlglot/") or rel.startswith("tests/"))
        ):
            self.verifiable = False
        return self._file(fp)

    def _file(self, fp):
        mp = self.mp
        if fp.binary:
            return ALL, f"binary/rename {fp.old}"
        rel = fp.old or fp.new
        if fp.old is None:
            # brand-new file: nothing imports it yet unless it shadows something
            if rel.endswith(".py") and (rel.startswith("sqlglot/") or rel.startswith("tests/")):
                base = os.path.basename(rel)
                if base == "conftest.py" or base == "__init__.py":
                    return ALL, f"new {rel}"
                # a new sqlglot module could be picked up by pkgutil walks / entry points
                return mp.touch("tests/test_typing.py") | mp.opened(rel), f"new {rel}"
            return 0, f"new {rel}"
        if fp.new is None or fp.old != fp.new:
            if rel.startswith("sqlglot/") or rel.startswith("tests/") or rel.startswith("sqlglotc/"):
                return ALL, f"delete/move {rel}"
            return mp.opened(rel), f"delete {rel}"

        bits = mp.opened(rel)
        for imp in mp.import_opened.get(rel, ()):
            if imp == "?":
                return ALL, f"import-opened {rel}"
            bits |= mp.file_bits.get(imp, 0) | mp.touch(imp)

        base = os.path.basename(rel)
        if base in ("pyproject.toml", "setup.cfg", "pytest.ini", "tox.ini", "conftest.py"):
            return ALL, f"config {rel}"
        if not rel.endswith(".py"):
            # fixtures and other data: the tests that read them (old text not needed)
            if not fp.hunks:
                return ALL, f"no-hunks {rel}"
            return bits, f"data {rel}"
        if rel.startswith("sqlglotc/"):
            return bits | mp.file_bits.get("tests/test_sqlglotc.py", 0), f"sqlglotc {rel}"
        if not (rel.startswith("sqlglot/") or rel.startswith("tests/")):
            return bits, f"other-py {rel}"

        old = self.old_text(rel)
        if old is None:
            return ALL, f"missing {rel}"
        try:
            new, removed, added, paired = apply_hunks(old, fp.hunks)
        except ValueError as e:
            return ALL, f"apply {rel}: {e}"
        if new == old:
            return bits, f"noop {rel}"
        b, w = self.python(rel, old, new, removed, added, paired)
        if b == ALL:
            return ALL, w
        if rel.startswith("sqlglot/"):
            self.patched[rel] = new
        self.all_new[rel] = new
        return bits | b, w

    def python(self, rel, old, new, removed, added, paired):
        mp = self.mp
        try:
            old_tree = ast.parse(old)
        except SyntaxError:
            return ALL, f"old-syntax {rel}"
        try:
            new_tree = ast.parse(new)
        except (SyntaxError, ValueError):
            return ALL, f"syntax-error {rel}"
        if ast.dump(old_tree) == ast.dump(new_tree):
            return 0, f"comment {rel}"

        bits = 0
        kinds = set()
        levels = []  # import-time loci: (path_tuple, top_stmt)
        old_defs = None
        narrow = set()  # expression classes named by the edited table entries
        narrow_ok = True
        attrs = set()  # class attributes whose value was edited
        attrs_ok = True
        old_lines = old.split("\n")
        new_lines = new.split("\n")
        pair_of = self._pairs(removed, added, paired)

        for ln in removed:
            loc = locate(old_tree, ln)
            if loc.func is not None and loc.in_body:
                b, k = self.body_line(rel, loc, ln)
                bits |= b | self.import_effects(rel, ln)
                kinds.add(k)
                continue
            other = pair_of.get(("-", ln))
            lam = None
            if other is not None:
                c0, c1 = changed_cols(old_lines[ln - 1], new_lines[other - 1])
                lam = self.toplevel_lambda(loc, ln, c0, c1)
            if lam is not None:
                bits |= self.lambda_bits(rel, lam)
                kinds.add("lambda")
                continue
            if loc.func is not None:  # def header
                bits |= self.func_bits(rel, loc.func)
                levels.append((tuple(loc.path[:-1]), loc.stmts[0] if loc.stmts else None))
                kinds.add("header")
                narrow_ok = False
            else:
                levels.append((tuple(loc.path), loc.stmts[0] if loc.stmts else None))
                kinds.add("class" if loc.path else "module")
                a = attr_target(loc.stmts[0] if loc.stmts else None) if loc.path else None
                if a is None:
                    attrs_ok = False
                else:
                    attrs.add(a)
                if narrow_ok:
                    other = pair_of.get(("-", ln))
                    cls = None
                    if other is not None:
                        c0, c1 = changed_cols(old_lines[ln - 1], new_lines[other - 1])
                        cls = self.entry_classes(loc.stmts[0] if loc.stmts else None, ln, c0, c1)
                    if cls is None:
                        narrow_ok = False
                    else:
                        narrow |= cls

        for ln in added:
            loc = locate(new_tree, ln)
            if loc.func is not None and loc.in_body:
                if ln in paired:
                    continue
                if old_defs is None:
                    old_defs = index_defs(old_tree)
                node = old_defs.get(tuple(loc.path))
                if node is None or not isinstance(node, FUNC):
                    levels.append((tuple(p for p in loc.path if p[0] == "class")[: self._cls_depth(loc.path)], None))
                    kinds.add("newdef")
                    narrow_ok = False
                    continue
                bits |= self.func_bits(rel, node)
                lo = node.body[0].lineno if node.body else node.lineno
                for oln in range(lo, node.end_lineno + 1):
                    bits |= self.import_effects(rel, oln)
                kinds.add("insert")
                continue
            if ln in paired:
                c0, c1 = changed_cols(old_lines[paired[ln] - 1], new_lines[ln - 1], new_side=True)
                lam = self.toplevel_lambda(loc, ln, c0, c1)
                if lam is not None:
                    # the edit sits inside a lambda body in the new code; the old side
                    # was handled by its removed line (lambda or table rule)
                    continue
            if loc.func is not None:
                if old_defs is None:
                    old_defs = index_defs(old_tree)
                node = old_defs.get(tuple(loc.path))
                if node is not None:
                    bits |= self.func_bits(rel, node)
                levels.append((tuple(loc.path[:-1]), None))
                kinds.add("header")
                narrow_ok = False
            else:
                if ln in paired and loc.path:
                    # same class body edit already recorded from the removed side
                    levels.append((tuple(loc.path), None))
                else:
                    levels.append((tuple(loc.path), loc.stmts[0] if loc.stmts else None))
                kinds.add("class" if loc.path else "module")
                a = attr_target(loc.stmts[0] if loc.stmts else None) if loc.path else None
                if a is None:
                    attrs_ok = False
                else:
                    attrs.add(a)
                if narrow_ok:
                    other = pair_of.get(("+", ln))
                    cls = None
                    if other is not None:
                        c0, c1 = changed_cols(old_lines[other - 1], new_lines[ln - 1], new_side=True)
                        cls = self.entry_classes(loc.stmts[0] if loc.stmts else None, ln, c0, c1)
                    if cls is None:
                        narrow_ok = False
                    else:
                        narrow |= cls

        seen = set()
        lvl = 0
        all_tracked = True
        for path, stmt in levels:
            key = (path, id(stmt) if stmt is not None else None)
            if key in seen:
                continue
            seen.add(key)
            lvl |= self.level_bits(rel, old_tree, path, stmt)
            if not self.tracked_class(rel, path):
                all_tracked = False
        if levels and attrs_ok and attrs and all_tracked and NARROW_ATTRS:
            # data attributes of tracked classes are read by name (self.X, cls.X,
            # Base.X, getattr(x, "X")): keep tests that ran code naming them
            ab = 0
            for a in attrs:
                ab |= self.attr_users(a)
            lvl &= ab
            kinds.add("attr")
        if levels:
            self.import_level = True
        if levels and narrow_ok and narrow:
            nb = 0
            for c in narrow:
                nb |= self.mp.expr_bits(c)
            lvl &= nb
            kinds.add("entry")
        bits |= lvl
        return bits, f"{'/'.join(sorted(kinds))} {rel}"

    @staticmethod
    def _pairs(removed, added, paired):
        out = {}
        for new_ln, old_ln in paired.items():
            out[("+", new_ln)] = old_ln
            out[("-", old_ln)] = new_ln
        return out

    def tracked_class(self, rel, path):
        classes = [n for k, n in path if k == "class"]
        if not classes or len(classes) != len(path):
            return False
        return f"{module_name(rel)}:{'.'.join(classes)}" in self.mp.types

    def attr_users(self, name):
        """Tests that ran anything naming `name` (any module): functions, class bodies
        (users of that class), module level (users of that module)."""
        mp = self.mp
        key = ("@attr", name)
        if key in mp._refs_cache:
            return mp._refs_cache[key]
        bits = 0
        for r, kind, val in mp.refs.get(name, ()):
            if kind == "f":
                bits |= mp.line(r, -val)
            elif kind == "c":
                t = mp.types.get(f"{module_name(r)}:{val}")
                bits |= mp.sets[t] if t is not None else mp.touch(r)
            else:
                bits |= mp.touch(r)
        mp._refs_cache[key] = bits
        return bits

    def entry_classes(self, stmt, ln, c0, c1):
        """Expression classes named by the one table entry holding the edit, else None.

        The edit (columns c0..c1 of line ln) must sit inside a single element of a
        literal dict/set/list/tuple assigned at class or module level, and must not
        touch a string key (a renamed key could collide with another entry)."""
        if stmt is None or not isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            return None
        value = stmt.value
        if isinstance(value, ast.Call) and len(value.args) == 1 and not value.keywords:
            value = value.args[0]
        elems = []
        if isinstance(value, ast.Dict):
            for k, v in zip(value.keys, value.values):
                elems.append((k, v))
        elif isinstance(value, (ast.Set, ast.List, ast.Tuple)):
            for e in value.elts:
                elems.append((None, e))
        else:
            return None
        for k, v in elems:
            start = k if k is not None else v
            if not _within(start, v, ln, c0, c1):
                continue
            if k is None and isinstance(v, ast.Starred):
                return None
            if k is not None and isinstance(k, ast.Constant) and _within(k, k, ln, c0, c1, loose=True):
                return None  # edit touches a string/constant key
            if k is None and isinstance(value, ast.Dict):
                return None  # **spread
            # every `exp.X` the entry mentions must be an expression class; an entry
            # keyed by anything else (exp.DType.X, strings...) cannot be narrowed
            cls = set()
            for node in (k, v):
                if node is None:
                    continue
                for n in ast.walk(node):
                    if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "exp":
                        if not self.mp.is_expr_class(n.attr):
                            return None
                        cls.add(n.attr)
            return cls or None
        return None

    @staticmethod
    def _cls_depth(path):
        # number of leading class entries before the first def
        k = 0
        for kind, _ in path:
            if kind != "class":
                break
            k += 1
        return k

    def func_bits(self, rel, node):
        mp = self.mp
        return mp.entry(rel, node) | mp.span(rel, first_line(node), node.end_lineno)

    @staticmethod
    def toplevel_lambda(loc, ln, c0, c1):
        """Innermost lambda whose body holds the edited columns c0..c1 of line ln,
        for class/module level code. An edit to the table key or anything else on
        the same line is not a lambda-body edit."""
        if loc.func is not None:
            return None
        roots = loc.stmts[-1:] if loc.stmts else []
        best = None
        for root in roots:
            for n in ast.walk(root):
                if isinstance(n, ast.Lambda) and _within(n.body, n.body, ln, c0, c1):
                    if best is None or (n.end_lineno - n.lineno, n.end_col_offset - n.col_offset) < (
                        best.end_lineno - best.lineno, best.end_col_offset - best.col_offset
                    ):
                        best = n
        return best

    def lambda_bits(self, rel, lam):
        mp = self.mp
        return mp.line(rel, -lam.lineno) | mp.span(rel, lam.lineno + 1, lam.end_lineno)

    def refs_bits(self, name, rel):
        """Users of `name` defined in `rel`: references in rel itself or in modules
        that import rel (by any form of import)."""
        mp = self.mp
        cache = mp._refs_cache
        key = (name, rel)
        if key in cache:
            return cache[key]
        ok = set(mp.static_importers.get(rel, ()))
        ok.add(rel)
        bits = 0
        for r, kind, val in mp.refs.get(name, ()):
            if r not in ok:
                continue
            if kind == "f":
                bits |= mp.line(r, -val)
            elif kind == "c":
                t = mp.types.get(f"{module_name(r)}:{val}")
                bits |= mp.sets[t] if t is not None else mp.touch(r)
            else:
                bits |= mp.touch(r)
        cache[key] = bits
        return bits

    def level_bits(self, rel, tree, path, stmt):
        """Import-time change at class body `path` (tuple of ("class", name)) or module level."""
        mp = self.mp
        bits = 0
        mod = module_name(rel)
        classes = [n for k, n in path if k == "class"]
        if rel.startswith("sqlglot/") and rel in mp.collect:
            bits |= mp.lazy_bits
        if rel.startswith("tests/"):
            # a test module that fails to import fails every file importing it
            bits |= mp.file_bits.get(rel, 0) | mp.touch(rel)
            for imp in mp.static_importers.get(rel, ()):
                bits |= mp.file_bits.get(imp, 0) | mp.touch(imp)
        if classes and len(classes) == len(path):
            qual = ".".join(classes)
            t = mp.types.get(f"{mod}:{qual}")
            node = index_defs(tree).get(tuple(path))
            if node is not None:
                for n in ast.walk(node):
                    if isinstance(n, FUNC):
                        bits |= self.func_bits(rel, n)
                    elif isinstance(n, ast.Lambda):
                        bits |= self.lambda_bits(rel, n)
            for imp in mp.name_importers.get((rel, classes[0]), ()):
                bits |= mp.touch(imp)
            if t is not None:
                return bits | mp.sets[t]
            # a class we do not track instances of: anything using the module or the name
            return bits | mp.touch(rel) | self.refs_bits(classes[0], rel) | self.refs_bits(classes[-1], rel)
        if path:
            # class nested in a function etc.: be broad
            return bits | mp.touch(rel) | self.importers_touch(rel)
        bits |= mp.touch(rel)
        names, simple = bound_names(stmt) if stmt is not None else (set(), False)
        for nm in names:
            bits |= self.refs_bits(nm, rel)
            for imp in mp.name_importers.get((rel, nm), ()):
                bits |= mp.touch(imp)
        if not simple:
            bits |= self.importers_touch(rel)
        return bits

    def importers_touch(self, rel):
        mp = self.mp
        bits = 0
        for imp in mp.static_importers.get(rel, ()):
            bits |= mp.touch(imp)
        return bits

    def body_line(self, rel, loc, ln):
        mp = self.mp
        stmts = loc.stmts
        if not stmts:
            # comment / blank line inside a body, or the line holds only a def's body start
            return mp.entry(rel, loc.func) | mp.span(rel, first_line(loc.func), loc.func.end_lineno), "body-gap"
        s = stmts[-1]
        if isinstance(s, COMPOUND):
            lo = first_line(s)
            b = mp.span(rel, lo, s.end_lineno)
            if isinstance(s, FUNC) or isinstance(s, ast.ClassDef):
                # nested def/class header: runs whenever the outer function runs
                b |= self.enclosing(rel, loc, len(stmts) - 1)
            return b, "compound"
        if is_invisible(s):
            return self.enclosing(rel, loc, len(stmts) - 1), "invisible"
        b = mp.span(rel, first_line(s), s.end_lineno)
        return b, "stmt"

    def enclosing(self, rel, loc, idx):
        """Coverage of the block holding stmts[idx]: parent stmt span, or the function."""
        mp = self.mp
        if idx > 0:
            p = loc.stmts[idx - 1]
            return mp.span(rel, first_line(p), p.end_lineno)
        f = loc.func
        return mp.entry(rel, f) | mp.span(rel, first_line(f), f.end_lineno)

    def import_effects(self, rel, ln):
        """If this line runs while some module is imported, that module's users."""
        mp = self.mp
        imps = mp.imports.get(rel, {}).get(ln)
        if not imps:
            return 0
        self.import_level = True
        bits = 0
        for imp in imps:
            if imp == "?" or imp == rel:
                continue
            bits |= mp.touch(imp)
            if imp.startswith("tests/"):
                bits |= mp.file_bits.get(imp, 0)
        return bits


# --------------------------------------------------------------------------- workers


def plan(nums, ms):
    if not nums:
        return []
    items = sorted(nums, key=lambda n: -ms[n])
    total = sum(ms[n] for n in items)
    best = None
    for w in range(1, MAX_WORKERS + 1):
        if w > len(items):
            break
        if best is not None and total / w + WORKER_MS * w > best[0] and ms[items[0]] + WORKER_MS * w > best[0]:
            if w * WORKER_MS > best[0]:
                break
        loads = [0] * w
        bins = [[] for _ in range(w)]
        for n in items:
            k = loads.index(min(loads))
            loads[k] += ms[n]
            bins[k].append(n)
        cost = max(loads) + WORKER_MS * w
        if best is None or cost < best[0]:
            best = (cost, bins)
    return best[1]


# --------------------------------------------------------------------------- verification
#
# Listing a test costs its recorded seconds on the busiest worker plus 2 s per worker.
# Running it here costs our CPU seconds. For small selections, running the tests on
# the patched tree and listing only the ones that really fail is cheaper, and a
# commit where nothing fails then costs only the CPU it took to find out.

VERIFY = os.environ.get("BR_VERIFY", "1") == "1"
RUNNER_DIR = HERE  # brrun.py lives next to this file
HEAVY_FILES = ("tests/test_optimizer.py", "tests/test_executor.py")
VERIFY_CAP_S = float(os.environ.get("BR_VERIFY_CAP", "40"))
DEADLINE_S = float(os.environ.get("BR_DEADLINE", "150"))
SLOW_FACTOR = 20.0
HEAVY_S = float(os.environ.get("BR_HEAVY_S", "5"))
WITNESSES = int(os.environ.get("BR_WITNESSES", "25"))
USE_SERVER = os.environ.get("BR_SERVER", "1") == "1"  # tests this slow are listed, not run
# Drop heavy tests when every light selected test passes (a measured bet; see README)
DROP_HEAVY = os.environ.get("BR_DROP_HEAVY", "1") == "1"


def plan_cost(nums, ms):
    w = plan(sorted(nums), ms)
    if not w:
        return 0
    return max(sum(ms[n] for n in b) for b in w) + WORKER_MS * len(w)


class ForkServer:
    """Client for brserver.py (one process, forks a pytest child per job)."""

    def __init__(self):
        import subprocess
        import threading

        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": "/tmp",
            "PYTHONPATH": str(RUNNER_DIR),
            "PYTHONDONTWRITEBYTECODE": "1",
            "SKIP_INTEGRATION": "0",
            "TMPDIR": "/tmp",
        }
        self.proc = subprocess.Popen([sys.executable, str(RUNNER_DIR / "brserver.py")],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, env=env, cwd="/tmp")
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
        self.results = {}
        self.next_id = 0
        self.alive = True
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        for line in self.proc.stdout:
            try:
                r = json.loads(line)
            except Exception:
                continue
            with self.cond:
                self.results[r["id"]] = r
                self.cond.notify_all()
        with self.cond:
            self.alive = False
            self.cond.notify_all()

    def run(self, root, argfile, result, timeout):
        import time

        with self.cond:
            self.next_id += 1
            jid = self.next_id
            msg = json.dumps({"id": jid, "root": root, "argfile": argfile, "result": result,
                              "timeout": timeout}) + "\n"
            self.proc.stdin.write(msg.encode())
            self.proc.stdin.flush()
            end = time.monotonic() + timeout + 15
            while jid not in self.results:
                if not self.alive or time.monotonic() > end:
                    return None
                self.cond.wait(0.5)
            return self.results.pop(jid)

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()


class Workspace:
    """A writable copy of the pinned tree under /tmp (fixtures symlinked)."""

    def __init__(self, root, server=None):
        import shutil

        self.server = server
        self.last_cpu = 0.0
        self.root = root
        if os.path.exists(root):
            shutil.rmtree(root)
        os.makedirs(root)
        for sub in ("sqlglot", "sqlglotc"):
            if (SRC / sub).exists():
                shutil.copytree(SRC / sub, os.path.join(root, sub),
                                ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(SRC / "tests", os.path.join(root, "tests"),
                        ignore=shutil.ignore_patterns("__pycache__", "fixtures"))
        os.symlink(SRC / "tests" / "fixtures", os.path.join(root, "tests", "fixtures"))
        self.dirty = {}

    def apply(self, files):
        for rel, text in files.items():
            path = os.path.join(self.root, rel)
            self.dirty[rel] = os.path.exists(path)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)

    def restore(self):
        import shutil

        for rel, existed in self.dirty.items():
            path = os.path.join(self.root, rel)
            if existed:
                shutil.copyfile(SRC / rel, path)
            elif os.path.exists(path):
                os.remove(path)
        self.dirty = {}

    def run(self, ids, timeout):
        """Run node ids; return (failed_ids, seen_ids, collect_failed, durations) or None."""
        import subprocess

        res = os.path.join(self.root, "result.json")
        if os.path.exists(res):
            os.remove(res)
        argf = os.path.join(self.root, "args.txt")
        with open(argf, "w") as f:
            f.write("\n".join(ids))
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": "/tmp",
            "PYTHONPATH": f"{self.root}:{RUNNER_DIR}",
            "PYTHONDONTWRITEBYTECODE": "1",
            "SKIP_INTEGRATION": "0",
            "BR_RESULT": res,
            "TMPDIR": "/tmp",
        }
        if self.server is not None:
            got = self.server.run(self.root, argf, res, timeout)
            if got is None or got.get("status") == -1:
                log("verify run failed/timed out")
                return None
            self.last_cpu = got.get("cpu", 0.0)
            return self._read(res)
        cmd = [sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", "-p", "brrun",
               "-o", "addopts=", "--timeout", "60", "-W", "ignore", f"@{argf}"]
        try:
            proc = subprocess.Popen(cmd, cwd=self.root, env=env, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
            import time

            t_end = time.monotonic() + timeout
            while True:
                pid, status, ru = os.wait4(proc.pid, os.WNOHANG)
                if pid:
                    break
                if time.monotonic() > t_end:
                    proc.kill()
                    os.wait4(proc.pid, 0)
                    log("verify run timed out")
                    return None
                time.sleep(0.02)
            self.last_cpu = ru.ru_utime + ru.ru_stime
        except Exception as e:
            log("verify run failed:", e)
            return None
        return self._read(res)

    def _read(self, res):
        try:
            with open(res) as f:
                r = json.load(f)
        except Exception:
            return None
        sq = r.get("sqlglot") or ""
        if not sq.startswith(self.root + os.sep):
            log("verify: wrong sqlglot imported:", sq)
            return None
        return set(r["failed"]), set(r["seen"]), set(r["collect_failed"]), r.get("durations", {})


class Verifier:
    def __init__(self, tests, ms):
        self.ms = ms
        self.id_of = {t["number"]: t["id"] for t in tests}
        self.secs = {t["number"]: t["seconds"] for t in tests}
        # in-process runtimes measured on the pinned commit (the recorded seconds of
        # executor/optimizer tests include process-pool startup we do not pay)
        try:
            with open(HERE / "durations.json") as f:
                d = json.load(f)
        except Exception:
            d = {}
        self.run_secs = {n: d.get(i, self.secs[n]) for n, i in self.id_of.items()}
        self.ok = False
        self.t0 = None
        try:
            import time

            self.t0 = time.monotonic()
            self.server = ForkServer() if USE_SERVER else None
            base = os.environ.get("BR_TMP", "/tmp")
            self.ws = [Workspace(os.path.join(base, f"brws{k}"), self.server) for k in range(2)]
            self.ok = self.canary()
        except Exception as e:
            log("verifier disabled:", e)
            self.ok = False
        log("verifier", "on" if self.ok else "off")

    def canary(self):
        """The runner must see a failure we plant, and a pass when there is none."""
        num = next((n for n, i in self.id_of.items()
                    if i == "tests/dialects/test_dremio.py::TestDremio::test_regexp_split"), None)
        if num is None:
            return False
        ws = self.ws[0]
        good = ws.run([self.id_of[num]], 60)
        rel = "sqlglot/parsers/dremio.py"
        ws.apply({rel: "raise RuntimeError('canary')\n" + (SRC / rel).read_text(encoding="utf-8")})
        try:
            bad = ws.run([self.id_of[num]], 60)
        finally:
            ws.restore()
        return (good is not None and not good[0] and self.id_of[num] in good[1]
                and bad is not None and self.id_of[num] in bad[0])

    def estimate(self, nums):
        files = {self.id_of[n].split("::", 1)[0] for n in nums}
        est = 0.35 + 0.85 * sum(self.run_secs[n] for n in nums)
        if any(f in HEAVY_FILES for f in files):
            est += 1.8
        return est

    def split(self, nums):
        heavy = {n for n in nums if self.secs[n] >= HEAVY_S or self.run_secs[n] >= HEAVY_S}
        return set(nums) - heavy, heavy

    def witnesses(self, h, light):
        """Cheapest light selected tests from the heavy test's own file."""
        f = self.id_of[h].split("::", 1)[0]
        same = sorted((n for n in light if self.id_of[n].split("::", 1)[0] == f),
                      key=lambda n: (self.run_secs[n], n))
        return set(same[:WITNESSES])

    def choose(self, nums):
        """Pick what to run: nothing, the heavy tests' witnesses, or every light test.
        Returns (run_set, expected_cost_if_nothing_fails) or None for 'list statically'."""
        if not self.ok or not nums:
            return None
        light, heavy = self.split(nums)
        static = plan_cost(nums, self.ms) / 1000.0
        best = (static, None)
        droppable = {h for h in heavy if DROP_HEAVY and self.secs[h] >= HEAVY_S and self.witnesses(h, light)}
        kept = heavy - droppable
        if light:
            est = self.estimate(light)
            if est <= VERIFY_CAP_S:
                c = est + plan_cost(kept, self.ms) / 1000.0
                if c + 0.25 < best[0]:
                    best = (c, set(light))
        if droppable:
            wit = set()
            for h in droppable:
                wit |= self.witnesses(h, light)
            c = self.estimate(wit) + plan_cost((light - wit) | kept, self.ms) / 1000.0
            if c + 0.25 < best[0]:
                best = (c, wit)
        return None if best[1] is None else best

    def worth(self, nums):
        return self.choose(nums) is not None

    def time_left(self):
        import time

        return DEADLINE_S - (time.monotonic() - self.t0)

    def verify(self, ws, files, nums):
        """Return the tests to list, or None to keep the static answer."""
        if self.time_left() < 5:
            return None
        ch = self.choose(nums)
        if ch is None:
            return None
        _, run = ch
        light, heavy = self.split(nums)
        # suite order (tests.json is in collection order): a test that only fails after
        # an earlier one has run keeps that ordering here
        order = sorted(run)
        ids = [self.id_of[n] for n in order]
        est = self.estimate(run)
        ws.apply(files)
        try:
            r = ws.run(ids, timeout=min(self.time_left(), max(30.0, est * 4 + 20)))
        finally:
            ws.restore()
        if r is None:
            return None
        failed, seen, collect_failed, durs = r
        passed = set()
        for n in run:
            i = self.id_of[n]
            f = i.split("::", 1)[0]
            if (i in seen and i not in failed and f not in collect_failed
                    and durs.get(i, 0.0) <= SLOW_FACTOR * max(self.secs[n], self.run_secs[n]) + 10.0):
                passed.add(n)
            elif os.environ.get("BR_DEBUG"):
                log("  not passed:", i, "seen" if i in seen else "unseen",
                    "failed" if i in failed else "", f"dur={durs.get(i, 0.0):.2f}")
        live = passed != set(run)
        out = set(nums) - passed
        if not live and DROP_HEAVY:
            for h in heavy:
                if self.secs[h] < HEAVY_S:
                    continue  # cheap to list: never bet on it
                w = self.witnesses(h, light)
                if w and w <= passed:
                    out.discard(h)
        return out


def main():
    tests_doc = json.loads((DATA / "tests.json").read_text())
    tests = tests_doc["tests"]
    ms = {t["number"]: int(round(t["seconds"] * 1000)) for t in tests}
    every = [t["number"] for t in tests]
    try:
        mp = Map(MAP, tests)
    except Exception as e:  # map missing: be safe
        log("map load failed:", e)
        mp = None
    an = Analyzer(mp) if mp is not None else None
    if mp is not None:
        # bit for the subprocess-based lazy-load test
        lb = 0
        num_to_id = {t["number"]: t["id"] for t in tests}
        for b, num in enumerate(mp.num_of_bit):
            if num is not None and num_to_id[num] == "tests/dialects/test_dialect.py::TestDialect::test_lazy_load":
                lb |= 1 << b
        mp.lazy_bits = lb

    results = []  # (cid, nums, why, files, verifiable)
    for patch in sorted((DATA / "commits").glob("*.patch")):
        cid = patch.stem
        try:
            text = patch.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            text = None
        files, verifiable = {}, False
        if an is None or text is None:
            nums = every
            why = "fallback"
        else:
            try:
                bits, why = an.commit(text)
                files, verifiable = dict(an.all_new), an.verifiable
            except Exception as e:  # never crash: run everything
                bits, why = ALL, f"error {e!r}"
            if bits == ALL:
                nums = every
                verifiable = False
            else:
                nums = set(mp.unmapped)
                nb = mp.num_of_bit
                while bits:
                    low = bits & -bits
                    i = low.bit_length() - 1
                    n = nb[i]
                    if n is not None:
                        nums.add(n)
                    bits ^= low
                nums = sorted(nums)
        results.append([cid, nums, why, files, verifiable])

    if VERIFY and an is not None:
        todo = [r for r in results if r[4] and r[3] and r[1]]
        ver = Verifier(tests, ms) if todo else None
        if ver is not None and ver.ok:
            todo = [r for r in todo if ver.worth(r[1])]
            # cheapest first, so the deadline cuts the least valuable work
            todo.sort(key=lambda r: ver.choose(r[1])[0])
            import threading

            lock = threading.Lock()
            queue = list(todo)

            def worker(ws):
                while True:
                    with lock:
                        if not queue:
                            return
                        r = queue.pop(0)
                    got = ver.verify(ws, r[3], r[1])
                    if got is not None:
                        log(f"{r[0]}: verified {len(r[1])} -> {len(got)} listed; "
                            f"cpu {ws.last_cpu:.2f}s static {plan_cost(r[1], ms)/1000:.2f}s "
                            f"-> {plan_cost(got, ms)/1000:.2f}s")
                        r[1] = sorted(got)
                        r[2] += ";verified"

            threads = [threading.Thread(target=worker, args=(w,)) for w in ver.ws]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        if ver is not None and getattr(ver, "server", None) is not None:
            ver.server.close()

    out = []
    for cid, nums, why, files, verifiable in results:
        workers = plan(nums, ms)
        log(f"{cid}: {len(nums)} tests, {why}")
        out.append(json.dumps({"commit": cid, "workers": workers}, separators=(",", ":")))
    sys.stdout.write("\n".join(out) + ("\n" if out else ""))


if __name__ == "__main__":
    main()
