#!/usr/bin/env python3
# -*- coding=utf-8 -*-

"""Mercurial ``.hg`` disclosure dumper."""

import re
import os
import struct
import zlib
from urllib.parse import quote

import click
from aiomultiprocess import Pool

from ..dumper import BaseDumper


class Dumper(BaseDumper):
    """Best-effort dumper for exposed Mercurial repositories."""

    REVLOG_ENTRY = struct.Struct(">Qiiiiii20s12x")
    REVLOG_ENTRY_SIZE = REVLOG_ENTRY.size

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

        self.recover_store_data_files()

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

    def recover_store_data_files(self):
        """Recover newest file contents from downloaded .hg/store/data revlogs."""
        store_data = os.path.join(self.outdir, ".hg", "store", "data")
        if not os.path.isdir(store_data):
            return

        for root, _dirs, files in os.walk(store_data):
            for name in files:
                if not name.endswith(".i"):
                    continue
                index_path = os.path.join(root, name)
                store_path = os.path.relpath(index_path, os.path.join(self.outdir, ".hg", "store"))
                store_path = store_path.replace(os.sep, "/")
                source_name = self.source_name_from_store_path(store_path)
                if not source_name:
                    continue
                data_path = index_path[:-2] + ".d"
                data_data = None
                if os.path.isfile(data_path):
                    with open(data_path, "rb") as f:
                        data_data = f.read()
                try:
                    contents = self.recover_latest_revlog_file(
                        self.read_file(index_path), data_data=data_data
                    )
                except Exception as e:
                    self.error_log("Failed to recover Mercurial revlog %s" % store_path, e=e)
                    continue
                if contents is None:
                    continue
                self.save_recovered_file(source_name, contents)

    def read_file(self, filename: str) -> bytes:
        with open(filename, "rb") as f:
            return f.read()

    def save_recovered_file(self, filename: str, data: bytes):
        fullname = os.path.abspath(os.path.join(self.outdir, filename))
        outdir = os.path.abspath(self.outdir)
        if os.path.commonpath([outdir, fullname]) != outdir:
            click.secho("Skip unsafe recovered Mercurial path: %s" % filename, fg="red")
            return

        target = fullname
        if os.path.exists(target):
            try:
                with open(target, "rb") as f:
                    if f.read() == data:
                        return
            except Exception:
                pass
            target = os.path.abspath(os.path.join(self.outdir, ".hg", "recovered", filename))
            if os.path.exists(target):
                return

        self.makedirs(target)
        with open(target, "wb") as f:
            f.write(data)
        shown = os.path.relpath(target, self.outdir).replace(os.sep, "/")
        click.secho("[HG] recovered %s" % shown, fg="green")

    def recover_latest_revlog_file(self, index_data: bytes, data_data: bytes = None):
        revisions = self.parse_revlog(index_data, data_data=data_data)
        if not revisions:
            return None
        return revisions[-1]

    def parse_revlog(self, index_data: bytes, data_data: bytes = None) -> list:
        entries = self.parse_revlog_entries(index_data, data_data=data_data)
        fulltexts = []
        for rev, entry in enumerate(entries):
            chunk = self.decompress_revlog_chunk(entry["chunk"])
            if entry["base_rev"] == rev or entry["base_rev"] < 0:
                fulltext = chunk
            elif entry["base_rev"] < len(fulltexts):
                try:
                    fulltext = self.apply_revlog_delta(fulltexts[entry["base_rev"]], chunk)
                except Exception:
                    fulltext = chunk
            else:
                fulltext = chunk
            fulltexts.append(fulltext)
        return fulltexts

    def parse_revlog_entries(self, index_data: bytes, data_data: bytes = None) -> list:
        if data_data is None:
            inline_entries = self.parse_inline_revlog_entries(index_data)
            if inline_entries is not None:
                return inline_entries
        return self.parse_split_revlog_entries(index_data, data_data or b"")

    def parse_inline_revlog_entries(self, data: bytes):
        pos = 0
        entries = []
        while pos < len(data):
            if pos + self.REVLOG_ENTRY_SIZE > len(data):
                return None
            entry = self.unpack_revlog_entry(data[pos : pos + self.REVLOG_ENTRY_SIZE])
            compressed_len = entry["compressed_len"]
            if compressed_len < 0:
                return None
            chunk_start = pos + self.REVLOG_ENTRY_SIZE
            chunk_end = chunk_start + compressed_len
            if chunk_end > len(data):
                return None
            entry["chunk"] = data[chunk_start:chunk_end]
            entries.append(entry)
            pos = chunk_end
        return entries

    def parse_split_revlog_entries(self, index_data: bytes, data_data: bytes) -> list:
        entries = []
        entry_count = len(index_data) // self.REVLOG_ENTRY_SIZE
        for rev in range(entry_count):
            start = rev * self.REVLOG_ENTRY_SIZE
            entry = self.unpack_revlog_entry(index_data[start : start + self.REVLOG_ENTRY_SIZE])
            compressed_len = max(entry["compressed_len"], 0)
            chunk_start = entry["offset"]
            chunk_end = chunk_start + compressed_len
            entry["chunk"] = data_data[chunk_start:chunk_end]
            entries.append(entry)
        return entries

    def unpack_revlog_entry(self, data: bytes) -> dict:
        (
            offset_flags,
            compressed_len,
            _uncompressed_len,
            base_rev,
            _link_rev,
            _p1_rev,
            _p2_rev,
            _node,
        ) = self.REVLOG_ENTRY.unpack(data)
        return {
            "offset": offset_flags & 0x0000FFFFFFFFFFFF,
            "flags": offset_flags >> 48,
            "compressed_len": compressed_len,
            "base_rev": base_rev,
            "chunk": b"",
        }

    def decompress_revlog_chunk(self, chunk: bytes) -> bytes:
        if not chunk:
            return b""
        if chunk.startswith(b"u"):
            return chunk[1:]
        if chunk.startswith(b"\0"):
            return chunk
        return zlib.decompress(chunk)

    def apply_revlog_delta(self, base: bytes, delta: bytes) -> bytes:
        output = []
        pos = 0
        delta_pos = 0
        while delta_pos + 12 <= len(delta):
            start, end, data_len = struct.unpack(">lll", delta[delta_pos : delta_pos + 12])
            delta_pos += 12
            if start < pos or end < start or data_len < 0:
                raise ValueError("invalid Mercurial delta")
            output.append(base[pos:start])
            output.append(delta[delta_pos : delta_pos + data_len])
            delta_pos += data_len
            pos = end
        if delta_pos != len(delta):
            raise ValueError("trailing Mercurial delta bytes")
        output.append(base[pos:])
        return b"".join(output)
