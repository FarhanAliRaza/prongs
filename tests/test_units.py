"""units.py: what a change to a file means, unit by unit, and the selection
rules built on it (formatting is no change; a binding nothing observes
selects nothing; a changed test selects itself)."""

from __future__ import annotations

import ast

from conftest import T_A, T_B, T_C

from prongs import units

OLD = '''"""Module docstring."""
import os
from typing import Iterator

LIMIT = 10


def f(a):
    """Add one."""
    return a + 1


class C:
    size = 1

    def m(self):
        def inner():
            return 1
        return inner()
'''


class Project:
    """A project with one other module, pkg/other.py, loaded at collection."""

    star = False
    used = False

    def module_state(self, path, node, alias):
        name = node.module if isinstance(node, ast.ImportFrom) else alias.name
        return {"pkg.other": "loaded", "pkg.plugin": "unloaded"}.get(name, "external")

    def still_imported(self, path, node, alias):
        return self.module_state(path, node, alias) != "unloaded"

    def provides(self, path, node, alias):
        return alias.name != "missing"

    def star_imported(self, path, name):
        return self.star

    def used_elsewhere(self, path, name):
        return self.used


def delta(new: str, project: Project | None = None, **kw) -> dict:
    d = units.delta(OLD, new, "pkg/mod.py", project or Project(), **kw)
    return {k: sorted(v) for k, v in vars(d).items() if v and k != "scopes"}


def scopes(new: str) -> set[str]:
    return units.delta(OLD, new, "pkg/mod.py", Project()).scopes


def test_scopes_whose_names_changed_are_reported_for_tests_that_list_them():
    assert scopes(OLD.replace("return a + 1", "return a + 2")) == set()
    assert scopes(OLD.replace("LIMIT = 10\n", "LIMIT = 10\n\n\ndef g(): pass\n")) == {""}
    assert scopes(OLD.replace("from typing import Iterator\n", "")) == {""}
    assert scopes(OLD.replace("    def m(self):", "    def n(self): pass\n\n    def m(self):")) == {"C"}
    # the same names, one rebound: a listing can show a different object
    assert scopes(OLD.replace("size = 1", "size = 2")) == {"C"}


def test_formatting_comments_and_reexport_aliases_are_not_a_change():
    new = OLD.replace("return a + 1", "return (a +\n            1)  # sum")
    new = new.replace("import os\n", "import os  # noqa\n\n")
    new = new.replace("import Iterator", "import Iterator as Iterator")
    assert new != OLD and delta(new) == {}


def test_a_docstring_is_a_change_unless_configured_away():
    new = OLD.replace("Add one.", "Add 1.")
    assert delta(new) == {"changed": ["f"]}
    assert delta(new, ignore_docstrings=True) == {}
    new = OLD.replace("Module docstring.", "The module.")
    assert delta(new) == {"module_level": [
        "Expr statement (line 1) added or changed", "Expr statement (line 1) removed"]}
    assert delta(new, ignore_docstrings=True) == {}


def test_a_function_body_is_its_own_unit():
    # the nested function changed: neither its parent nor the class did
    assert delta(OLD.replace("return 1\n", "return 2\n")) == {"changed": ["C.m.<locals>.inner"]}
    # a signature is the function's header: the function changed, nothing else
    assert delta(OLD.replace("def f(a):", "def f(a, b=2):")) == {
        "changed": ["f"], "headers": ["f"]}
    # a removed function changed, for the tests that entered it
    assert delta(OLD.replace('def f(a):\n    """Add one."""\n    return a + 1\n', "")) == {
        "changed": ["f"], "headers": ["f"]}


def test_bindings_nothing_observes_are_additive():
    new = OLD.replace("LIMIT = 10\n", (
        "LIMIT = 10\n_PATTERN = re.compile('x')\n_NAMES = frozenset({'a', 'b'})\n\n\n"
        "def _helper(sep=os.sep):\n    return sep\n"))
    new = new.replace("import os\n", "import os\nimport re\nfrom pkg.other import thing\n")
    new = new.replace("import Iterator", "import Callable, Iterator")
    assert delta(new) == {"additive": [
        "new assignment to _NAMES (line 9)",
        "new assignment to _PATTERN (line 8)",
        "new def _helper (line 12)",
        "new import of Callable (line 5)",
        "new import of re (line 3)",
        "new import of thing (line 4)",
    ]}


def test_a_new_binding_something_can_observe_is_not_additive():
    def added(statement: str, project: Project | None = None) -> dict:
        return delta(OLD.replace("LIMIT = 10\n", f"LIMIT = 10\n{statement}\n"), project)

    # code that runs at import
    assert added("X = f(1)") == {"module_level": [
        "new assignment to X (line 6): its value is not a plain expression"]}
    assert added("@register\ndef g(): pass") == {"added": ["g"]}
    assert added("def g(x=f(1)): pass") == {"added": ["g"]}
    assert added("class D: pass") == {"module_level": ["class D (line 6) added or changed"]}
    assert added("import pkg.plugin") == {"module_level": [
        "new import of pkg (line 6): it imports a project module nothing imported before"]}
    assert added("from pkg.other import missing") == {"module_level": [
        "new import of missing (line 6): the module it names does not define it"]}
    # a name already in play: rebinding, shadowing a lookup, a dunder
    assert added("LIMIT = 11") == {"module_level": [
        "new assignment to LIMIT (line 6): the name was already bound"]}
    assert added("def f(a): return a") == {"changed": ["f"], "headers": ["f"]}
    assert added("__all__ = ['f']") == {"module_level": [
        "new assignment to __all__ (line 6): a dunder name is looked up by the interpreter"]}
    old = OLD.replace("return a + 1", "return len(a)")
    d = units.delta(old, old.replace("LIMIT = 10\n", "LIMIT = 10\ndef len(x): return 0\n"),
                    "pkg/mod.py", Project())
    assert d.added == {"len"} and not d.additive  # f looked `len` up as a builtin
    # a public name of a module some file star-imports
    star = Project()
    star.star = True
    assert added("def g(): pass", star) == {"added": ["g"]}
    assert added("def _g(): pass", star) == {"additive": ["new def _g (line 6)"]}


def test_class_level_changes_stay_module_level():
    assert delta(OLD.replace("size = 1", "size = 2")) == {"module_level": [
        "assignment to size (line 14) in class C added or changed",
        "assignment to size (line 14) in class C removed"]}
    # a new method can override an inherited one: broadened, as before
    assert delta(OLD.replace("    def m(self):", "    def n(self): pass\n\n    def m(self):")) == {
        "added": ["C.n"]}
    assert delta(OLD.replace("class C:", "class C(Base):")) == {"module_level": [
        "class C (line 13) added or changed", "class C (line 13) removed"]}


def test_removing_an_import_nothing_refers_to_is_additive():
    new = OLD.replace("from typing import Iterator\n", "")
    assert delta(new) == {"additive": ["removed import of Iterator (line 3)"]}
    used = Project()
    used.used = True
    assert delta(new, used) == {"module_level": [
        "removed import of Iterator (line 3): another file takes it from this module"]}
    # still referred to: by code, or by a string (__all__, getattr)
    old = OLD.replace("return a + 1", "return os.getcwd()")
    d = units.delta(old, old.replace("import os\n", ""), "pkg/mod.py", Project())
    assert d.module_level == ["removed import of os (line 2): the module still refers to it"]


def test_reordering_statements_is_module_level_but_reordering_functions_is_not():
    # imports can only fail by being reordered, which an import check shows
    new = OLD.replace("import os\nfrom typing import Iterator\n",
                      "from typing import Iterator\nimport os\n")
    assert delta(new) == {"additive": ["imports reordered"]}
    new = OLD.replace("LIMIT = 10\n", "LIMIT = 10\nLIMIT = 11\n")
    d = units.delta(new, new.replace("LIMIT = 10\nLIMIT = 11\n", "LIMIT = 11\nLIMIT = 10\n"),
                    "pkg/mod.py", Project())
    assert d.module_level == ["statements reordered"] and not d.additive
    moved = OLD.replace('def f(a):\n    """Add one."""\n    return a + 1\n\n\n', "") + (
        '\n\ndef f(a):\n    """Add one."""\n    return a + 1\n')
    assert delta(moved) == {}


def test_a_file_that_does_not_parse_has_no_delta():
    assert units.delta(OLD, "def f(:\n", "pkg/mod.py", Project()) is None


def test_import_candidates_resolve_relative_imports_exactly():
    def cands(path: str, stmt: str) -> list[str]:
        return units.import_candidates(path, ast.parse(stmt).body[0])

    assert cands("a/b/c.py", "import x.y") == ["x.py", "x/__init__.py", "x/y.py", "x/y/__init__.py"]
    assert cands("a/b/c.py", "from . import d") == ["=a/b/d.py", "=a/b/d/__init__.py", "=a/b/__init__.py"]
    assert cands("a/b/c.py", "from ..x import y") == [
        "=a/x.py", "=a/x/__init__.py", "=a/x.py", "=a/x/__init__.py", "=a/x/y.py",
        "=a/x/y/__init__.py", "=a/__init__.py"]
    assert units.matches("x/y.py", "src/x/y.py") and not units.matches("x/y.py", "src/ax/y.py")
    assert units.matches("=a/x.py", "a/x.py") and not units.matches("=a/x.py", "src/a/x.py")


# --- selection ---------------------------------------------------------------

def test_formatting_only_edits_select_nothing(recorded):
    recorded.edit("pkg/mod.py", "return a() + b()", "return (a() +\n            b())  # sum")
    recorded.edit("tests/test_m.py", "assert a() == 1", "assert a() == 1  # one")
    sel = recorded.select()
    assert sel["mode"] == "select" and sel["selected"] == {} and sel["targets"] == []
    assert sel["changed_functions"] == [] and sel["import_check"] == []


def test_docstring_edits_follow_the_setting(recorded):
    recorded.edit("pkg/mod.py", "def b():\n", 'def b():\n    """Two."""\n')
    assert set(recorded.select()["selected"]) == {T_B, T_C}
    recorded.write("pyproject.toml", recorded.read("pyproject.toml")
                   + "\n[tool.prongs]\nignore_docstrings = true\ninert = ['pyproject.toml']\n")
    assert recorded.select()["selected"] == {}


def test_unobserved_bindings_select_nothing_and_ask_for_an_import_check(recorded):
    recorded.edit("pkg/mod.py", "def a():", "import re\n\n_WORD = re.compile('x')\n\n\n"
                  "def _helper():\n    return _WORD\n\n\ndef a():")
    sel = recorded.select()
    assert sel["mode"] == "select" and sel["selected"] == {}
    assert sel["units"] == {
        "module_level": {},
        "additive": {"pkg/mod.py": [
            "new assignment to _WORD (line 3)", "new def _helper (line 6)",
            "new import of re (line 1)"]},
        "unexecuted": [],
    }
    assert sel["import_check"] == ["tests/test_m.py"]
    out = recorded.cli("run")
    assert out["summary"]["status"] == "nothing_to_run"
    assert out["import_check"]["ok"] is True and out["import_check"]["exit"] == 0
    assert out["skip_receipt"]["additive"] == sel["units"]["additive"]
    # the new helper gets a caller: that caller is the change
    recorded.edit("pkg/mod.py", "return 2", "return 2 if _helper() else 0")
    assert set(recorded.select()["selected"]) == {T_B, T_C}


def test_an_unobserved_binding_that_breaks_the_import_runs_everything(recorded):
    recorded.edit("pkg/mod.py", "def a():", "import re\n\n_WORD = re.compile('(')\n\n\ndef a():")
    assert recorded.select()["selected"] == {}  # statically, nothing observes it
    out = recorded.cli("run", code=2)
    assert out["import_check"]["ok"] is False and out["import_check"]["exit"] == 2
    assert out["selection"]["mode"] == "run_all"
    assert out["selection"]["run_all_reasons"] == [
        "import check failed: collecting the suite exited 2"]
    assert out["summary"]["status"] == "error"


def test_removing_an_unused_import_selects_nothing(recorded):
    recorded.edit("pkg/mod.py", "def a():", "import os\nimport pkg.extra\n\n\ndef a():")
    recorded.write("pkg/extra.py", "VALUE = 1\n")
    recorded.commit("imports")
    assert recorded.record().returncode == 0
    recorded.edit("pkg/mod.py", "import os\n", "")
    sel = recorded.select()
    assert sel["selected"] == {} and sel["units"]["additive"] == {
        "pkg/mod.py": ["removed import of os (line 1)"]}
    # the only import of a project module: its import-time code would stop running
    recorded.edit("pkg/mod.py", "import pkg.extra\n", "")
    sel = recorded.select()
    assert sel["mode"] == "run_all" and sel["units"]["module_level"] == {"pkg/mod.py": [
        "removed import of pkg (line 2): nothing else imports that project module"]}
    # another collected file imports it too: nothing is lost
    recorded.git("checkout", "--", "pkg/mod.py")
    recorded.edit("tests/test_m.py", "from pkg.mod import a, b, c",
                  "import pkg.extra\nfrom pkg.mod import a, b, c")
    recorded.commit("the tests import extra")
    assert recorded.record().returncode == 0
    recorded.edit("pkg/mod.py", "import pkg.extra\n", "")
    sel = recorded.select()
    assert sel["selected"] == {} and sel["units"]["additive"] == {
        "pkg/mod.py": ["removed import of pkg (line 2)"]}


def test_a_name_another_file_imports_is_not_removable(recorded):
    recorded.edit("pkg/mod.py", "def a():", "from os import sep\n\n\ndef a():")
    recorded.write("pkg/user.py", "from pkg.mod import sep\n")
    recorded.commit("re-export")
    assert recorded.record().returncode == 0
    recorded.edit("pkg/mod.py", "from os import sep\n", "")
    sel = recorded.select()
    assert sel["mode"] == "run_all" and sel["units"]["module_level"] == {"pkg/mod.py": [
        "removed import of sep (line 1): another file takes it from this module"]}


def test_a_changed_function_no_test_enters_selects_nothing_from_the_evidence_tree(recorded):
    recorded.edit("pkg/mod.py", "def c():", "def unused():\n    return 4\n\n\ndef c():")
    recorded.commit("an unused function")
    assert recorded.record().returncode == 0
    recorded.edit("pkg/mod.py", "return 4", "return 5")
    sel = recorded.select()
    assert sel["selected"] == {} and sel["broadened"] == []
    assert sel["units"]["unexecuted"] == ["pkg/mod.py::unused"]
    # from an explicit base the map may be older than the function: broadened
    recorded.commit("change it")
    sel = recorded.select(base="HEAD~1")
    assert set(sel["selected"]) == {T_A, T_B, T_C} and sel["units"]["unexecuted"] == []


def test_a_star_importer_observes_a_new_name_only_if_it_uses_it(recorded):
    recorded.write("pkg/shim.py", "from pkg.mod import *\n")
    recorded.edit("tests/test_m.py", "from pkg.mod import a, b, c",
                  "import pkg.shim\nfrom pkg.mod import a, b, c")
    recorded.commit("a shim star-imports the module")
    assert recorded.record().returncode == 0
    recorded.edit("pkg/mod.py", "def c():", "def helper():\n    return 4\n\n\ndef c():")
    sel = recorded.select()
    assert sel["selected"] == {} and sel["units"]["additive"] == {
        "pkg/mod.py": ["new def helper (line 7)"]}
    # the shim already had a `helper` of its own: the star import now replaces it
    recorded.git("checkout", "--", "pkg/mod.py")
    recorded.write("pkg/shim.py", "helper = None\nfrom pkg.mod import *\n")
    recorded.commit("the shim binds helper")
    assert recorded.record().returncode == 0
    recorded.edit("pkg/mod.py", "def c():", "def helper():\n    return 4\n\n\ndef c():")
    sel = recorded.select()
    assert set(sel["selected"]) == {T_A, T_B, T_C} and sel["units"]["additive"] == {}


def test_py_typed_is_inert(recorded):
    recorded.write("pkg/py.typed", "")
    recorded.commit("ship the marker")
    assert recorded.select()["selected"] == {}
