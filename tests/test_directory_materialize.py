"""Directory ``materialize()`` — zip on client, unzip on worker."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

from pymonik import Materialize, task
from pymonik.blob import _zip_directory
from pymonik.testing import LocalCluster


@task
def list_files_under(p: Path) -> list[str]:
    return sorted(str(f.relative_to(p)) for f in p.rglob("*") if f.is_file())


@task
def read_text_at(p: Path, rel: str) -> str:
    return (p / rel).read_text()


@task
def read_file(p: Path) -> str:
    return p.read_text()


def _make_tree(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "a.txt").write_text("alpha")
    (root / "sub").mkdir()
    (root / "sub" / "b.txt").write_text("beta")
    (root / "sub" / "c.txt").write_text("gamma")


def test_zip_directory_is_deterministic(tmp_path):
    """Same content → same SHA, regardless of filesystem walk order."""
    a = tmp_path / "a"
    b = tmp_path / "b"
    _make_tree(a)
    _make_tree(b)
    assert _zip_directory(a) == _zip_directory(b)


def test_zip_directory_round_trip(tmp_path):
    src = tmp_path / "tree"
    _make_tree(src)
    data = _zip_directory(src)
    # It's a real zip.
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = sorted(zf.namelist())
    assert names == ["a.txt", "sub/b.txt", "sub/c.txt"]


def test_directory_materialize_end_to_end(tmp_path):
    src = tmp_path / "assets"
    _make_tree(src)
    target = tmp_path / "worker_assets"

    with LocalCluster() as client:
        with client.session() as s:
            handle = Materialize.__new__(Materialize)  # placeholder
            mat = __import__("pymonik").blob.materialize(src, at=str(target))
            assert mat.is_dir is True
            files = list_files_under.spawn(mat).result(timeout=30)
            assert files == ["a.txt", "sub/b.txt", "sub/c.txt"]
            txt = read_text_at.spawn(mat, "sub/b.txt").result(timeout=30)
            assert txt == "beta"


def test_file_materialize_still_works(tmp_path):
    src = tmp_path / "config.toml"
    src.write_text("[ok]\n")
    target = tmp_path / "worker_config.toml"

    with LocalCluster() as client:
        with client.session() as s:
            mat = __import__("pymonik").blob.materialize(src, at=str(target))
            assert mat.is_dir is False
            assert read_file.spawn(mat).result(timeout=30) == "[ok]\n"
