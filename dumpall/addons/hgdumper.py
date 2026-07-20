#!/usr/bin/env python3
# -*- coding=utf-8 -*-

"""Mercurial ``.hg`` disclosure dumper."""

import re
import struct
from urllib.parse import quote

import click
from aiomultiprocess import Pool

from ..dumper import BaseDumper


class Dumper(BaseDumper):
    """Best-effort dumper for exposed Mercurial repositories."""

    METADATA_FILES = (
        "requires",
        "branch",
        "bookmarks",
        "dirstate",
        "store/requires",
        "store/fncache",
        "store/00changelog.i",
        "store/00changelog.d",
        "store/00manifest.i",
        "store/00manifest.d",
        "store/phaseroots",
        "cache/branch2-served",
        "cache/tags2-visible",
    )

    def __init__(self, url: str, outdir: str, **kwargs):
        super(Dumper, self).__init__(url, outdir, **kwargs)
        self.base_url = re.sub(r"\.hg.*", ".hg", url).rstrip("/")
        self.root_url = re.sub(r"/?\.hg.*", "", url).rstrip("/")
        self.seen_targets = set()

    async def start(self):
        requires_url = self.base_url + "/requires"
        status, requires = await self.fetch(requires_url)
        if status != 200 or not requires:
            click.secho("Failed [%s] %s" % (status, requires_url), fg="red")
            return

        for name in self.METADATA_FILES:
            self.add_target(self.base_url + "/" + quote(name, safe="/"), ".hg/" + name)

        if b"dirstate-v2" in requires:
            click.secho(
                "Mercurial dirstate-v2 is not parsed; dumping known metadata only.",
                fg="yellow",
            )
        else:
            await self.collect_from_dirstate()

        await self.collect_from_fncache()

        if not self.targets:
            click.secho("No Mercurial files found.", fg="yellow")
            return

        async with Pool() as pool:
            await pool.map(self.download, self.targets)

    async def collect_from_dirstate(self):
        status, data = await self.fetch(self.base_url + "/dirstate")
        if status != 200 or not data:
            click.secho(
                "Failed [%s] %s" % (status, self.base_url + "/dirstate"), fg="red"
            )
            return

        for filename in self.parse_dirstate(data):
            await self.add_working_file(filename)
            await self.add_store_candidates(filename)

    async def collect_from_fncache(self):
        status, data = await self.fetch(self.base_url + "/store/fncache")
        if status != 200 or not data:
            return

        for store_path in self.parse_fncache(data):
            url = self.base_url + "/store/" + quote(store_path, safe="/~")
            self.add_target(url, ".hg/store/" + store_path)
            filename = self.source_name_from_store_path(store_path)
            if filename:
                await self.add_working_file(filename)

    async def add_working_file(self, filename: str):
        if not filename or filename.startswith(".hg/"):
            return
        url = self.root_url + "/" + quote(filename, safe="/")
        if not self.force and not await self.checkit(url, filename):
            return
        self.add_target(url, filename)

    async def add_store_candidates(self, filename: str):
        for store_path in self.store_data_paths(filename):
            url = self.base_url + "/store/" + quote(store_path, safe="/~")
            local_path = ".hg/store/" + store_path
            if not self.force and not await self.checkit(url, local_path):
                continue
            self.add_target(url, local_path)

    def add_target(self, url: str, filename: str):
        key = (url, filename)
        if key in self.seen_targets:
            return
        self.seen_targets.add(key)
        self.targets.append(key)

    def parse_dirstate(self, data: bytes) -> list:
        """Parse Mercurial dirstate v1 and return tracked filenames."""
        if len(data) < 40:
            return []
        offset = 40  # two 20-byte parent node ids
        filenames = []
        while offset + 17 <= len(data):
            try:
                state, _mode, _size, _mtime, name_len = struct.unpack(
                    ">cllll", data[offset : offset + 17]
                )
            except struct.error:
                break
            offset += 17
            if name_len < 0 or offset + name_len > len(data):
                break
            raw_name = data[offset : offset + name_len]
            offset += name_len
            if state == b"r":
                continue
            raw_name = raw_name.split(b"\0", 1)[0]
            filename = raw_name.decode("utf-8", errors="surrogateescape").strip("/")
            if filename:
                filenames.append(filename)
        return filenames

    def parse_fncache(self, data: bytes) -> list:
        paths = []
        for line in data.decode("utf-8", errors="ignore").splitlines():
            path = line.strip().lstrip("/")
            if path and not path.startswith("../"):
                paths.append(path)
        return paths

    def store_data_paths(self, filename: str) -> list:
        """Return likely Mercurial revlog paths for a working-tree file."""
        normalized = filename.strip("/").replace("\\", "/")
        encoded = self.encode_store_path(normalized)
        paths = []
        for name in dict.fromkeys((normalized, encoded)):
            paths.append("data/%s.i" % name)
            paths.append("data/%s.d" % name)
        return paths

    def encode_store_path(self, path: str) -> str:
        """Small subset of Mercurial store encoding used when fncache is absent."""
        encoded = []
        for ch in path:
            code = ord(ch)
            if ch == "/":
                encoded.append(ch)
            elif ch == "_":
                encoded.append("__")
            elif "A" <= ch <= "Z":
                encoded.append("_" + ch.lower())
            elif code < 32 or ch in '\\:*?"<>|':
                encoded.append("~%02x" % code)
            else:
                encoded.append(ch)
        return "".join(encoded)

    def source_name_from_store_path(self, store_path: str) -> str:
        if not store_path.startswith("data/"):
            return ""
        for suffix in (".i", ".d"):
            if store_path.endswith(suffix):
                return self.decode_store_path(store_path[5 : -len(suffix)])
        return ""

    def decode_store_path(self, path: str) -> str:
        """Decode the common reversible parts of Mercurial store encoding."""
        out = []
        i = 0
        while i < len(path):
            ch = path[i]
            if ch == "_" and i + 1 < len(path):
                nxt = path[i + 1]
                if nxt == "_":
                    out.append("_")
                elif "a" <= nxt <= "z":
                    out.append(nxt.upper())
                else:
                    out.append(ch)
                    i -= 1
                i += 2
                continue
            if ch == "~" and i + 2 < len(path):
                try:
                    out.append(chr(int(path[i + 1 : i + 3], 16)))
                    i += 3
                    continue
                except ValueError:
                    pass
            out.append(ch)
            i += 1
        return "".join(out)
