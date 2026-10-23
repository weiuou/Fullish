"""Real directory-FD file exchange tests; also runnable with stdlib unittest in WSL."""

import base64
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fullish import sandbox_files


class PathTests(unittest.TestCase):
    def test_reject_paths_outside_workspace(self):
        for path in (
            "",
            "/etc/passwd",
            "../secret",
            "a/../../secret",
            "/workspace/../x",
            "a//b",
            "./x",
            "a/./b",
            "a\\b",
            "x\x00",
            "x\n",
            "/workspace/",
        ):
            with self.subTest(path=path), self.assertRaises(ValueError):
                sandbox_files.relative_path(path)
        self.assertEqual(
            sandbox_files.relative_path("/workspace/nested/file.png"), "nested/file.png"
        )


@unittest.skipUnless(os.name == "posix", "Directory-FD exchange requires Linux")
class ExchangeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.root = self.directory / "workspace"
        self.root.mkdir()
        self.addCleanup(patch.stopall)
        patch.object(sandbox_files, "ROOT", self.root).start()
        self.body = b"\x89PNG\r\n\x1a\nreal-file-content"
        self.encoded = base64.b64encode(self.body).decode()

    def test_roundtrip_creates_parents_and_preserves_bytes(self):
        saved = sandbox_files.exchange("write", "imports/ref.png", self.encoded)
        self.assertEqual(saved, {"path": "imports/ref.png", "size": len(self.body)})
        read = sandbox_files.exchange("read", "/workspace/imports/ref.png")
        self.assertEqual(base64.b64decode(read["data"]), self.body)
        self.assertEqual(list((self.root / "imports").iterdir()), [self.root / "imports/ref.png"])

    def test_never_overwrites_existing_files_or_symlinks(self):
        outside = self.directory / "private"
        outside.write_bytes(b"keep")
        for kind in ("file", "symlink"):
            path = self.root / kind
            if kind == "file":
                path.write_bytes(b"keep")
            else:
                path.symlink_to(outside)
            with self.subTest(kind=kind), self.assertRaises(OSError):
                sandbox_files.exchange("write", kind, self.encoded)
            self.assertEqual(path.read_bytes(), b"keep")
        self.assertEqual(outside.read_bytes(), b"keep")
        self.assertFalse(list(self.root.glob(".upload-*")))

    def test_rejects_links_at_every_component(self):
        outside = self.directory / "private"
        outside.mkdir()
        (outside / "secret").write_bytes(b"secret")
        (self.root / "escape").symlink_to(outside, target_is_directory=True)
        (self.root / "secret-link").symlink_to(outside / "secret")
        for operation, path in (
            ("read", "escape/secret"),
            ("write", "escape/new"),
            ("read", "secret-link"),
        ):
            with self.subTest(operation=operation, path=path), self.assertRaises(OSError):
                sandbox_files.exchange(operation, path, self.encoded)
        self.assertFalse((outside / "new").exists())

    def test_rejects_fifo_hardlink_and_oversize_without_blocking(self):
        os.mkfifo(self.root / "pipe")
        (self.root / "ordinary").write_bytes(b"x")
        os.link(self.root / "ordinary", self.root / "hardlink")
        with (self.root / "large").open("wb") as stream:
            stream.truncate(sandbox_files.MAX_FILE_BYTES + 1)
        for name in ("pipe", "hardlink", "large"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                sandbox_files.exchange("read", name)


if __name__ == "__main__":
    unittest.main()
