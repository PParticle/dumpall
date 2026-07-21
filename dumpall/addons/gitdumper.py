#!/usr/bin/env python3
# -*- coding=utf-8 -*-

"""Git ``.git`` disclosure dumper."""

import os
import re
import zlib
from tempfile import NamedTemporaryFile
from urllib.parse import quote

import click
from aiomultiprocess import Pool

from ..dumper import BaseDumper
from ..thirdparty.gin import parse


class Dumper(BaseDumper):
    """Dump current worktree files and reconstruct exposed Git history."""

    METADATA_FILES = (
        "HEAD",
        "config",
        "COMMIT_EDITMSG",
        "description",
        "index",
        "packed-refs",
        "info/exclude",
        "objects/info/packs",
        "logs/HEAD",
    )
    COMMON_REFS = (
        "refs/heads/master",
        "refs/heads/main",
        "refs/heads/dev",
        "refs/heads/develop",
        "refs/remotes/origin/HEAD",
        "refs/remotes/origin/master",
        "refs/remotes/origin/main",
        "refs/remotes/origin/dev",
        "refs/remotes/origin/develop",
        "logs/refs/heads/master",
        "logs/refs/heads/main",
        "logs/refs/heads/dev",
        "logs/refs/heads/develop",
        "logs/refs/remotes/origin/HEAD",
        "logs/refs/remotes/origin/master",
        "logs/refs/remotes/origin/main",
        "logs/refs/remotes/origin/dev",
        "logs/refs/remotes/origin/develop",
    )
    SHA1_RE = re.compile(rb"\b[0-9a-f]{40}\b")

    def __init__(self, url: str, outdir: str, **kwargs):
        super(Dumper, self).__init__(url, outdir, **kwargs)
        self.base_url = re.sub(r"\.git.*", ".git", url).rstrip("/")
        self.worktree_targets = []
        self.seen_worktree_targets = set()
        self.seen_git_files = set()
        self.seen_objects = set()
        self.object_queue = []

    async def start(self):
        """入口方法"""
        await self.dump()

    async def dump(self):
        """Dump Git metadata, reachable objects, and index worktree files."""
        index_data = await self.collect_metadata_and_refs()
        if index_data:
            await self.collect_index_blobs(index_data)

        await self.collect_reachable_objects()

        if self.worktree_targets:
            async with Pool() as pool:
                await pool.map(self.download, self.worktree_targets)

    async def collect_metadata_and_refs(self) -> bytes:
        """Download known Git metadata and seed object traversal from refs/logs."""
        index_data = b""
        head_ref = ""

        for name in self.METADATA_FILES:
            status, data = await self.fetch_git_file(name, required=name == "HEAD")
            if status != 200 or data is None:
                continue
            self.seed_hashes(data)
            if name == "HEAD":
                head_ref = self.parse_head_ref(data)
            elif name == "index":
                index_data = data
            elif name == "objects/info/packs":
                await self.collect_pack_files(data)

        ref_paths = list(self.COMMON_REFS)
        if head_ref:
            ref_paths.extend([head_ref, "logs/" + head_ref])

        for name in dict.fromkeys(ref_paths):
            status, data = await self.fetch_git_file(name)
            if status == 200 and data:
                self.seed_hashes(data)

        return index_data

    async def collect_pack_files(self, data: bytes):
        """Download pack files advertised by objects/info/packs."""
        for pack_name in self.parse_info_packs(data):
            for suffix in (".pack", ".idx"):
                name = "objects/pack/%s%s" % (pack_name, suffix)
                await self.fetch_git_file(name)

    async def collect_index_blobs(self, index_data: bytes):
        """Parse .git/index, seed blob objects, and keep old worktree restore behavior."""
        with NamedTemporaryFile(mode="wb", delete=False) as f:
            f.write(index_data)
            idx_name = f.name
        try:
            try:
                for entry in parse(idx_name):
                    sha1 = entry.get("sha1", "").strip()
                    filename = entry.get("name", "").strip()
                    if not self.is_sha1(sha1) or not filename:
                        continue
                    self.add_object_sha(sha1)
                    url = self.object_url(sha1)
                    if not self.force and not await self.checkit(url, filename):
                        return
                    self.add_worktree_target(url, filename)
            except SystemExit as e:
                # gin is a CLI-oriented parser and exits for malformed input.
                # In auto mode a false-positive .git/index must be non-fatal.
                self.error_log("Failed to parse Git index", e=e)
            except Exception as e:
                self.error_log("Failed to parse Git index", e=e)
        finally:
            os.unlink(idx_name)

    async def collect_reachable_objects(self):
        """Fetch commits/trees/blobs reachable from refs, logs, packed-refs, index."""
        while self.object_queue:
            sha1 = self.object_queue.pop(0)
            if sha1 in self.seen_objects:
                continue
            self.seen_objects.add(sha1)
            status, compressed = await self.fetch_object(sha1)
            if status != 200 or not compressed:
                continue
            try:
                raw = zlib.decompress(compressed)
                obj_type, body = self.split_object(raw)
            except Exception as e:
                self.error_log("Failed to parse Git object %s" % sha1, e=e)
                continue

            if obj_type == b"commit":
                self.parse_commit(body)
            elif obj_type == b"tree":
                self.parse_tree(body)

    async def fetch_git_file(self, name: str, required: bool = False) -> tuple:
        url = self.base_url + "/" + quote(name, safe="/")
        status, data = await self.fetch(url)
        if status == 200 and data is not None:
            await self.save_raw(url, ".git/" + name, data, status=status)
        elif required:
            click.secho("Failed [%s] %s" % (status, url), fg="red")
        return status, data

    async def fetch_object(self, sha1: str) -> tuple:
        url = self.object_url(sha1)
        filename = ".git/objects/%s/%s" % (sha1[:2], sha1[2:])
        status, data = await self.fetch(url)
        if status == 200 and data is not None:
            await self.save_raw(url, filename, data, status=status)
        else:
            click.secho("Failed [%s] %s" % (status, url), fg="red")
        return status, data

    async def save_raw(self, url: str, filename: str, data: bytes, status: int = 200):
        if filename in self.seen_git_files:
            return
        if not self.force and not await self.checkit(url, filename):
            return
        fullname = os.path.abspath(os.path.join(self.outdir, filename))
        self.makedirs(fullname=fullname)
        try:
            with open(fullname, "wb") as f:
                f.write(data)
            self.seen_git_files.add(filename)
            click.secho("[%s] %s %s" % (status, url, filename), fg="green")
        except IsADirectoryError:
            pass
        except Exception as e:
            self.error_log("Failed to download file %s %s" % (url, filename), e=e)

    def seed_hashes(self, data: bytes):
        for match in self.SHA1_RE.findall(data):
            self.add_object_sha(match.decode("ascii"))

    def add_object_sha(self, sha1: str):
        if (
            self.is_sha1(sha1)
            and sha1 != "0" * 40
            and sha1 not in self.seen_objects
        ):
            self.object_queue.append(sha1)

    def add_worktree_target(self, url: str, filename: str):
        key = (url, filename)
        if key in self.seen_worktree_targets:
            return
        self.seen_worktree_targets.add(key)
        self.worktree_targets.append(key)
        self.targets.append(key)

    def parse_head_ref(self, data: bytes) -> str:
        text = data.decode("utf-8", errors="ignore").strip()
        prefix = "ref: "
        if text.startswith(prefix):
            return text[len(prefix) :].strip().lstrip("/")
        self.seed_hashes(data)
        return ""

    def parse_info_packs(self, data: bytes) -> list:
        packs = []
        for line in data.decode("utf-8", errors="ignore").splitlines():
            line = line.strip()
            match = re.search(r"(pack-[0-9a-f]{40})\.pack$", line)
            if match:
                packs.append(match.group(1))
        return packs

    def parse_commit(self, body: bytes):
        for line in body.splitlines():
            if line.startswith(b"tree ") or line.startswith(b"parent "):
                sha1 = line.split(maxsplit=1)[1].decode("ascii", errors="ignore")
                self.add_object_sha(sha1)

    def parse_tree(self, body: bytes):
        offset = 0
        while offset < len(body):
            nul = body.find(b"\0", offset)
            if nul < 0 or nul + 21 > len(body):
                break
            sha1 = body[nul + 1 : nul + 21].hex()
            self.add_object_sha(sha1)
            offset = nul + 21

    def split_object(self, raw: bytes) -> tuple:
        header, sep, body = raw.partition(b"\0")
        if not sep:
            raise ValueError("invalid loose object header")
        obj_type = header.split(b" ", 1)[0]
        return obj_type, body

    def object_url(self, sha1: str) -> str:
        return "%s/objects/%s/%s" % (self.base_url, sha1[:2], sha1[2:])

    def is_sha1(self, value: str) -> bool:
        return bool(re.fullmatch(r"[0-9a-f]{40}", value or ""))

    def convert(self, data: bytes) -> bytes:
        """用zlib对 index 中的 blob 对象进行解压，恢复工作区文件。"""
        if data:
            try:
                data = zlib.decompress(data)
                data = re.sub(rb"blob \d+\x00", b"", data)
            except Exception as e:
                self.error_log("Failed to convert data", e=e)
        return data
