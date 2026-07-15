import tempfile
from pathlib import Path
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch

from click.testing import CliRunner

import dumpall
from dumpall.addons import autodumper
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
