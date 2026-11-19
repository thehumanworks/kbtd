import io
import os
import stat
import tempfile
import unittest
from contextlib import chdir, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from kbtd.cli import main
from kbtd.storage import BoardFile


class CliTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.file = Path(temp.name) / "my board.md"

    def cli(self, *args, ok=True):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(args)
        if ok:
            self.assertEqual(code, 0, err.getvalue())
            return out.getvalue()
        self.assertNotEqual(code, 0)
        return err.getvalue()

    def test_end_to_end(self):
        path = str(self.file)
        self.cli("init", path)
        self.cli("add", "-f", path, "Tasks", "Backlog", "Design a prototype")
        self.cli("-f", path, "move", "1", "1", "1", "In Progress")
        self.cli("--file", path, "toggle", "Tasks", "2", "1")
        self.cli("-f", path, "edit", "1", "2", "1", "Design mockups")
        listing = self.cli("list", "Tasks", "In Progress", "-f", path)
        self.assertIn("## 2. In Progress", listing)
        self.assertIn("1. [x] Design mockups", listing)
        self.assertNotIn("Backlog", listing)
        self.assertIn("- [x] Design mockups", self.file.read_text())
        self.cli("-f", path, "delete", "1", "2", "1")
        self.assertNotIn("Design mockups", self.file.read_text())

    def test_cli_and_tui_share_default_file(self):
        with chdir(self.file.parent):
            self.cli("init")
            self.assertTrue(Path("TODO.md").is_file())
            self.assertIn("Backlog", self.cli("list"))
            self.assertIn("interactive terminal", self.cli(ok=False))
            self.assertIn("interactive terminal", self.cli("TODO.md", ok=False))

    def test_failed_commands_leave_file_untouched(self):
        path = str(self.file)
        self.assertIn("init", self.cli("-f", path, "list", ok=False))
        data = b"# Alpha\r\n## C\r\n- [ ] first\r\n## C\r\n- [x] second"
        self.file.write_bytes(data)
        for args in [
            ("init",), ("toggle", "Alpha", "C", "1"), ("delete", "1", "2", "0"),
            ("move", "1", "1", "1", "Unknown"), ("edit", "1", "1", "1", "a\nb"),
            ("toggle", "1", "1", "-1"), ("add", "2", "1", "third"),
        ]:
            with self.subTest(args=args):
                self.cli("--file", path, *args, ok=False)
                self.assertEqual(self.file.read_bytes(), data)
        self.cli("--file", path, "toggle", "1", "2", "1")
        self.assertEqual(self.file.read_bytes(), data.replace(b"[x]", b"[ ]"))

    def test_atomic_save_failure_cleans_up_without_truncation(self):
        source = BoardFile(self.file)
        source.create()
        original = self.file.read_bytes()
        doc = source.load().document
        doc = doc.add(doc.sections[0].columns[0], "new")
        with patch("kbtd.storage.os.replace", side_effect=PermissionError):
            with self.assertRaises(PermissionError):
                source.save(doc)
        self.assertEqual(self.file.read_bytes(), original)
        self.assertEqual(list(self.file.parent.iterdir()), [self.file])

    @unittest.skipUnless(os.name == "posix", "POSIX file permissions")
    def test_replace_preserves_permissions_and_rereads(self):
        source = BoardFile(self.file)
        source.create()
        self.file.chmod(0o640)
        before = source.load()
        doc = before.document.add(before.document.sections[0].columns[0], "new")
        with patch.object(source, "load", wraps=source.load) as load:
            after = source.save(doc)
        load.assert_called_once_with()
        self.assertEqual(after.document, doc)
        self.assertNotEqual(before.version, after.version)
        self.assertEqual(stat.S_IMODE(self.file.stat().st_mode), 0o640)
