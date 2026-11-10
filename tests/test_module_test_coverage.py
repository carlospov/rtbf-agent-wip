"""Repo-wide structural check: every `core/`/`plugins/` module ships with a test file.

Enforces R26 (`docs/planning/RESTRICTIONS.md`): no new module under `core/` or `plugins/` ships
without a corresponding test file. This walks `src/rtbf_agent/core/` and
`src/rtbf_agent/plugins/`, and for every `.py` file that isn't an `__init__.py` (init files
typically have nothing of their own to test), asserts a file named exactly `test_<module_name>.py`
exists somewhere under `tests/` — matching the convention already used by
`tests/core/test_models.py` (for `core/models.py`) and `tests/core/test_interfaces.py` (for
`core/interfaces.py`). The match is exact (not prefix-based) so that, e.g., a test file for
`foo_bar.py` can never be mistaken for coverage of an unrelated `foo.py`.

This only checks that a test file *exists*; it says nothing about coverage or quality (see #11).
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src" / "rtbf_agent"
TESTS_ROOT = REPO_ROOT / "tests"

CHECKED_PACKAGES = ("core", "plugins")


def _modules_requiring_tests(src_root: Path) -> list[Path]:
    """Every non-`__init__.py` `.py` file under `src_root/core` and `src_root/plugins`."""
    modules = []
    for package in CHECKED_PACKAGES:
        package_root = src_root / package
        if not package_root.is_dir():
            continue
        for path in sorted(package_root.rglob("*.py")):
            if path.name != "__init__.py":
                modules.append(path)
    return modules


def _missing_test_files(src_root: Path, tests_root: Path) -> list[Path]:
    """Modules (from `_modules_requiring_tests`) with no `test_<module>*.py` under `tests_root`."""
    test_file_names = (
        {p.name for p in tests_root.rglob("test_*.py")} if tests_root.is_dir() else set()
    )
    missing = []
    for module_path in _modules_requiring_tests(src_root):
        expected_name = f"test_{module_path.stem}.py"
        if expected_name not in test_file_names:
            missing.append(module_path)
    return missing


def test_every_core_and_plugin_module_has_a_test_file() -> None:
    missing = _missing_test_files(SRC_ROOT, TESTS_ROOT)
    assert not missing, (
        "Modules under core/ or plugins/ are missing a tests/**/test_<module>*.py file (R26): "
        f"{sorted(str(p.relative_to(REPO_ROOT)) for p in missing)}"
    )


def test_missing_test_file_check_catches_an_untested_module(tmp_path: Path) -> None:
    """Prove the check is load-bearing: it must fail on an untested module and pass once tested.

    Without this, `_missing_test_files` could vacuously pass (e.g. by silently returning `[]`)
    and `test_every_core_and_plugin_module_has_a_test_file` would never actually catch a real
    violation.
    """
    src_root = tmp_path / "src"
    tests_root = tmp_path / "tests"
    (src_root / "core").mkdir(parents=True)
    (src_root / "core" / "__init__.py").write_text("")
    (src_root / "core" / "widget.py").write_text("class Widget: ...\n")
    tests_root.mkdir()

    missing = _missing_test_files(src_root, tests_root)
    assert [p.name for p in missing] == ["widget.py"]

    (tests_root / "test_widget.py").write_text("def test_widget() -> None: ...\n")

    assert _missing_test_files(src_root, tests_root) == []


def test_missing_test_file_check_does_not_false_positive_on_stem_prefix_collision(
    tmp_path: Path,
) -> None:
    """A module whose name is a prefix of another module's must not "borrow" its test file.

    E.g. `core/foo.py` (untested) must not be reported as covered just because
    `tests/test_foo_bar.py` exists for an unrelated `core/foo_bar.py` — a naive
    `name.startswith(f"test_{stem}")` check would wrongly clear `foo.py` here.
    """
    src_root = tmp_path / "src"
    tests_root = tmp_path / "tests"
    (src_root / "core").mkdir(parents=True)
    (src_root / "core" / "__init__.py").write_text("")
    (src_root / "core" / "foo.py").write_text("class Foo: ...\n")
    (src_root / "core" / "foo_bar.py").write_text("class FooBar: ...\n")
    tests_root.mkdir()
    (tests_root / "test_foo_bar.py").write_text("def test_foo_bar() -> None: ...\n")

    missing = _missing_test_files(src_root, tests_root)

    assert [p.name for p in missing] == ["foo.py"]


def test_missing_test_file_check_allows_a_package_with_only_an_init_file(tmp_path: Path) -> None:
    """A `plugins/`-style subpackage with nothing but `__init__.py` requires no test file yet.

    Mirrors the currently-empty `src/rtbf_agent/plugins/llm/` directory, which other in-flight
    work will populate with real modules (each of which will then need its own test file).
    """
    src_root = tmp_path / "src"
    tests_root = tmp_path / "tests"
    (src_root / "plugins" / "llm").mkdir(parents=True)
    (src_root / "plugins" / "__init__.py").write_text("")
    (src_root / "plugins" / "llm" / "__init__.py").write_text("")
    tests_root.mkdir()

    assert _missing_test_files(src_root, tests_root) == []
