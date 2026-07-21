import tempfile
import struct
import zlib
from pathlib import Path
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch

from click.testing import CliRunner

import dumpall
from dumpall.addons import autodumper
from dumpall.addons.gitdumper import Dumper as GitDumper
from dumpall.addons.hgdumper import Dumper as HgDumper
from dumpall.addons.idxdumper import Dumper as IdxDumper
from dumpall.dumper import BaseDumper


class NormalizeBaseUrlTests(IsolatedAsyncioTestCase):
    def test_plain_target(self):
        self.assertEqual(
            autodumper.normalize_base_url("https://example.test/project/"),
            "https://example.test/project",
        )

    def test_artifact_target(self):
        self.assertEqual(
            autodumper.normalize_base_url(
                "https://example.test/project/.git/index?ignored=yes"
            ),
            "https://example.test/project",
        )

    def test_ds_store_target(self):
        self.assertEqual(
            autodumper.normalize_base_url(
                "https://example.test/project/.DS_Store?ignored=yes"
            ),
            "https://example.test/project",
        )

    def test_hg_artifact_target(self):
        self.assertEqual(
            autodumper.normalize_base_url(
                "https://example.test/project/.hg/store/fncache?ignored=yes"
            ),
            "https://example.test/project",
        )

    def test_similar_path_is_not_treated_as_artifact(self):
        self.assertEqual(
            autodumper.normalize_base_url("https://example.test/.github/"),
            "https://example.test/.github",
        )

    async def test_runs_every_addon_in_order(self):
        calls = []

        class FakeDumper:
            def __init__(self, url, outdir, **kwargs):
                calls.append((url, outdir, kwargs))

            async def start(self):
                return None

        addons = (
            ("Git", FakeDumper, ".git/"),
            ("Mercurial", FakeDumper, ".hg/"),
            ("SVN", FakeDumper, ".svn/"),
            ("DS_Store", FakeDumper, ".DS_Store"),
            ("Web index", FakeDumper, ""),
        )
        with patch.object(autodumper, "ADDONS", addons):
            dumper = autodumper.Dumper(
                "https://example.test/app/", "/tmp/output", debug=True
            )
            await dumper.start()

        self.assertEqual(
            [call[0] for call in calls],
            [
                "https://example.test/app/.git/",
                "https://example.test/app/.hg/",
                "https://example.test/app/.svn/",
                "https://example.test/app/.DS_Store",
                "https://example.test/app/",
            ],
        )
        self.assertTrue(all(call[2]["debug"] for call in calls))

    async def test_addon_failure_does_not_stop_later_addons(self):
        calls = []

        class BrokenDumper:
            def __init__(self, url, outdir, **kwargs):
                calls.append(("broken", url))

            async def start(self):
                raise ValueError("invalid artifact")

        class WorkingDumper:
            def __init__(self, url, outdir, **kwargs):
                calls.append(("working", url))

            async def start(self):
                return None

        addons = (
            ("Broken", BrokenDumper, ".git/"),
            ("Working", WorkingDumper, ".svn/"),
        )
        with patch.object(autodumper, "ADDONS", addons):
            dumper = autodumper.Dumper("https://example.test/", "/tmp/output")
            with patch.object(dumper, "error_log") as error_log:
                await dumper.start()

        self.assertEqual([call[0] for call in calls], ["broken", "working"])
        error_log.assert_called_once()


class CliTests(TestCase):
    def test_plain_url_uses_auto_dumper(self):
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(dumpall, "banner"), patch.object(
                dumpall, "start"
            ) as start:
                result = runner.invoke(
                    dumpall.main,
                    ["-u", "https://example.test/project/", "-o", temp_dir],
                )

            self.assertEqual(result.exit_code, 0, result.output)
            output_dir = Path(temp_dir) / "example.test_None"
            self.assertTrue(output_dir.is_dir())
            start.assert_called_once_with(
                "https://example.test/project/",
                str(output_dir),
                proxy="",
                force=False,
                debug=False,
            )


class IndexFileTests(IsolatedAsyncioTestCase):
    async def test_http_error_body_is_not_treated_as_index(self):
        dumper = BaseDumper("https://example.test", "/tmp/output")
        dumper.fetch = AsyncMock(return_value=(404, b"<html>not found</html>"))

        result = await dumper.indexfile("https://example.test/.git/index")

        self.assertIsNone(result)


class IndexDumperTests(TestCase):
    def test_directory_listing_url_is_saved_as_index_file(self):
        dumper = IdxDumper("https://example.test/", "/tmp/output")

        self.assertEqual(dumper.target_name_from_url("https://example.test/"), "index")
        self.assertEqual(
            dumper.target_name_from_url("https://example.test/.git/"),
            ".git/index",
        )
        self.assertEqual(
            dumper.target_name_from_url("https://example.test/.git/objects/pack/"),
            ".git/objects/pack/index",
        )
        self.assertEqual(
            dumper.target_name_from_url("https://example.test/flag.txt"),
            "flag.txt",
        )

    def test_web_index_skips_vcs_metadata_paths(self):
        dumper = IdxDumper("https://example.test/", "/tmp/output")

        self.assertTrue(dumper.should_skip_url("https://example.test/.git/"))
        self.assertTrue(dumper.should_skip_url("https://example.test/.hg/requires"))
        self.assertTrue(dumper.should_skip_url("https://example.test/.svn/entries"))
        self.assertFalse(dumper.should_skip_url("https://example.test/.github/"))


class GitDumperTests(IsolatedAsyncioTestCase):
    def git_object(self, obj_type: bytes, body: bytes) -> bytes:
        return zlib.compress(obj_type + b" " + str(len(body)).encode() + b"\0" + body)

    def test_parse_info_packs(self):
        dumper = GitDumper("https://example.test/.git/", "/tmp/output")

        self.assertEqual(
            dumper.parse_info_packs(b"P pack-" + b"4" * 40 + b".pack\n"),
            ["pack-" + "4" * 40],
        )

    async def test_collects_reachable_history_objects_from_refs(self):
        commit_sha = "1" * 40
        tree_sha = "2" * 40
        blob_sha = "3" * 40
        tree_body = b"100644 flag.txt\0" + bytes.fromhex(blob_sha)
        commit_body = b"tree " + tree_sha.encode() + b"\n\ninitial\n"

        with tempfile.TemporaryDirectory() as temp_dir:
            dumper = GitDumper("https://example.test/.git/", temp_dir, force=True)

            async def fake_fetch(url):
                mapping = {
                    "https://example.test/.git/HEAD": b"ref: refs/heads/master\n",
                    "https://example.test/.git/refs/heads/master": (
                        commit_sha.encode() + b"\n"
                    ),
                    "https://example.test/.git/objects/11/" + "1" * 38: self.git_object(
                        b"commit", commit_body
                    ),
                    "https://example.test/.git/objects/22/" + "2" * 38: self.git_object(
                        b"tree", tree_body
                    ),
                    "https://example.test/.git/objects/33/" + "3" * 38: self.git_object(
                        b"blob", b"flag{ok}\n"
                    ),
                }
                if url in mapping:
                    return (200, mapping[url])
                return (404, b"")

            dumper.fetch = AsyncMock(side_effect=fake_fetch)
            await dumper.collect_metadata_and_refs()
            await dumper.collect_reachable_objects()

            self.assertTrue((Path(temp_dir) / ".git/HEAD").is_file())
            self.assertTrue((Path(temp_dir) / ".git/refs/heads/master").is_file())
            self.assertTrue(
                (Path(temp_dir) / ".git/objects/11" / ("1" * 38)).is_file()
            )
            self.assertTrue(
                (Path(temp_dir) / ".git/objects/22" / ("2" * 38)).is_file()
            )
            self.assertTrue(
                (Path(temp_dir) / ".git/objects/33" / ("3" * 38)).is_file()
            )


class HgDumperTests(IsolatedAsyncioTestCase):
    def dirstate_entry(self, state: bytes, filename: bytes) -> bytes:
        return struct.pack(">cllll", state, 0, 0, 0, len(filename)) + filename

    def inline_revlog(self, contents: bytes) -> bytes:
        chunk = zlib.compress(contents)
        header = HgDumper.REVLOG_ENTRY.pack(
            0x0003000100000000,
            len(chunk),
            len(contents),
            0,
            0,
            -1,
            -1,
            b"1" * 20,
        )
        return header + chunk

    def test_parse_dirstate_v1(self):
        dumper = HgDumper("https://example.test/app/.hg/", "/tmp/output")
        data = (
            b"\0" * 40
            + self.dirstate_entry(b"n", b"index.php")
            + self.dirstate_entry(b"a", b"src/App.py")
            + self.dirstate_entry(b"r", b"deleted.txt")
        )

        self.assertEqual(dumper.parse_dirstate(data), ["index.php", "src/App.py"])

    def test_store_path_encoding_for_uppercase_files(self):
        dumper = HgDumper("https://example.test/app/.hg/", "/tmp/output")

        self.assertEqual(
            dumper.store_data_paths("src/App_File.py"),
            [
                "data/src/App_File.py.i",
                "data/src/App_File.py.d",
                "data/src/_app___file.py.i",
                "data/src/_app___file.py.d",
            ],
        )

    async def test_collects_targets_from_dirstate_and_fncache(self):
        dumper = HgDumper("https://example.test/app/.hg/", "/tmp/output", force=True)
        dirstate = b"\0" * 40 + self.dirstate_entry(b"n", b"index.php")

        async def fake_fetch(url):
            if url.endswith("/dirstate"):
                return (200, dirstate)
            if url.endswith("/store/fncache"):
                return (200, b"data/src/_app.py.i\n")
            return (404, b"")

        dumper.fetch = AsyncMock(side_effect=fake_fetch)
        await dumper.collect_from_dirstate()
        await dumper.collect_from_fncache()

        self.assertIn(("https://example.test/app/index.php", "index.php"), dumper.targets)
        self.assertIn(
            (
                "https://example.test/app/.hg/store/data/index.php.i",
                ".hg/store/data/index.php.i",
            ),
            dumper.targets,
        )
        self.assertIn(
            (
                "https://example.test/app/.hg/store/data/src/_app.py.i",
                ".hg/store/data/src/_app.py.i",
            ),
            dumper.targets,
        )
        self.assertIn(("https://example.test/app/src/App.py", "src/App.py"), dumper.targets)

    def test_recovers_inline_revlog_file(self):
        dumper = HgDumper("https://example.test/app/.hg/", "/tmp/output")

        self.assertEqual(
            dumper.recover_latest_revlog_file(self.inline_revlog(b"<?php flag();\n")),
            b"<?php flag();\n",
        )

    def test_recover_store_data_writes_missing_source_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            revlog_path = Path(temp_dir) / ".hg/store/data/flag.php.i"
            revlog_path.parent.mkdir(parents=True)
            revlog_path.write_bytes(self.inline_revlog(b"flag{ok}\n"))
            dumper = HgDumper("https://example.test/app/.hg/", temp_dir)

            dumper.recover_store_data_files()

            self.assertEqual((Path(temp_dir) / "flag.php").read_bytes(), b"flag{ok}\n")
