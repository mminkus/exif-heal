"""Tests for backup_file: relative-path preservation and root resolution."""

from pathlib import Path

from exif_heal.backup import backup_file


class TestBackupFile:

    def test_preserves_relative_path_under_root(self, tmp_path):
        root = tmp_path / "lib"
        (root / "Album").mkdir(parents=True)
        src = root / "Album" / "IMG_001.jpg"
        src.write_text("photo-bytes")
        backup = tmp_path / "bak"

        dest = backup_file(src, root, backup)

        assert dest == backup / "Album" / "IMG_001.jpg"
        assert dest.read_text() == "photo-bytes"

    def test_relative_root_is_resolved(self, tmp_path, monkeypatch):
        # Regression: a relative --root used to make relative_to fail and
        # collapse the backup to the bare filename.
        root = tmp_path / "lib"
        (root / "Album").mkdir(parents=True)
        src = root / "Album" / "IMG_001.jpg"
        src.write_text("x")
        backup = tmp_path / "bak"

        monkeypatch.chdir(root)
        dest = backup_file(src, Path("."), backup)

        # Structure preserved despite relative root.
        assert dest == backup / "Album" / "IMG_001.jpg"

    def test_same_name_different_subdirs_dont_collide(self, tmp_path):
        root = tmp_path / "lib"
        (root / "A").mkdir(parents=True)
        (root / "B").mkdir(parents=True)
        (root / "A" / "IMG.jpg").write_text("a")
        (root / "B" / "IMG.jpg").write_text("b")
        backup = tmp_path / "bak"

        backup_file(root / "A" / "IMG.jpg", root, backup)
        backup_file(root / "B" / "IMG.jpg", root, backup)

        assert (backup / "A" / "IMG.jpg").read_text() == "a"
        assert (backup / "B" / "IMG.jpg").read_text() == "b"

    def test_source_outside_root_mirrors_full_path(self, tmp_path):
        root = tmp_path / "lib"
        root.mkdir()
        outside = tmp_path / "elsewhere" / "IMG.jpg"
        outside.parent.mkdir()
        outside.write_text("x")
        backup = tmp_path / "bak"

        dest = backup_file(outside, root, backup)

        # Not flattened to the bare filename; full structure mirrored under backup.
        assert dest.name == "IMG.jpg"
        assert "elsewhere" in dest.parts
        assert dest.read_text() == "x"
