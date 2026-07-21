#!/usr/bin/env python
# -*- coding:utf-8 -*-
"""
@author: HJK
@file: dsdumper
@time: 2019-10-26
"""

"""
.DS_Store泄漏利用工具
递归解析.DS_Store并下载文件
"""

import re
import os
import asyncio
import click
from urllib.parse import urlparse
from asyncio.queues import Queue
from ..thirdparty import dsstore
from ..dumper import BaseDumper


class Dumper(BaseDumper):
    """ .DS_Store dumper """

    def __init__(self, url: str, outdir: str, **kwargs):
        super(Dumper, self).__init__(url, outdir, **kwargs)
        self.base_url = re.sub(r"/\.DS_Store.*", "", url)
        self.url_queue = Queue()
        self.seen_targets = set()
        self.seen_base_urls = set()
        self.ds_store_count = 0
        self.downloaded_count = 0

    async def start(self):
        """ dumper 入口方法 """
        # TODO：递归效率还可以优化，不过一般情况下已经够用
        await self.url_queue.put(self.base_url)
        # 递归解析.DS_Store，并把目标URL存到self.targets
        await self.parse_loop()

        await self.dump()
        self.found = bool(self.ds_store_count or self.targets)
        if self.found:
            self.summary = "%d .DS_Store file(s), %d/%d referenced file(s)" % (
                self.ds_store_count,
                self.downloaded_count,
                len(self.targets),
            )

    async def dump(self):
        # 创建协程池，调用download
        task_pool = []
        for target in self.targets:
            task_pool.append(asyncio.create_task(self.download(target)))
        for t in task_pool:
            if await t:
                self.downloaded_count += 1

    async def parse_loop(self):
        """ 从url_queue队列中读取URL，根据URL获取并解析DS_Store """
        while not self.url_queue.empty():
            base_url = await self.url_queue.get()
            if base_url in self.seen_base_urls:
                continue
            self.seen_base_urls.add(base_url)
            status, ds_data = await self.fetch(base_url + "/.DS_Store")
            if status != 200 or not ds_data:
                continue
            if self.is_html_response(ds_data):
                continue
            ds_filename = self.ds_store_output_name(base_url)
            self.save_raw(ds_filename, ds_data)
            self.ds_store_count += 1
            try:
                # 解析DS_Store
                ds = dsstore.DS_Store(ds_data)
                for filename in set(ds.traverse_root()):
                    await self.add_target(base_url, filename)
            except Exception as e:
                # Some CTF fixtures expose simplified textual DS_Store-like
                # content. Treat it as a leak and extract obvious filenames.
                for filename in self.parse_textual_listing(ds_data):
                    await self.add_target(base_url, filename, recurse=False)
                if self.debug:
                    msg = "Failed to parse ds_store file"
                    self.error_log(msg=msg, e=e)

    async def add_target(self, base_url: str, filename: str, recurse: bool = True):
        filename = filename.strip().strip("/")
        if not filename or filename in (".", ".."):
            return
        if self.is_unsafe_name(filename):
            if self.debug:
                click.secho("Skip unsafe DS_Store entry: %s" % filename, fg="yellow")
            return
        new_url = "%s/%s" % (base_url.rstrip("/"), filename)
        if recurse:
            await self.url_queue.put(new_url)
        fullname = urlparse(new_url).path.lstrip("/")
        key = (new_url, fullname)
        if key in self.seen_targets:
            return
        self.seen_targets.add(key)
        self.targets.append(key)

    def save_raw(self, filename: str, data: bytes):
        fullname = os.path.abspath(os.path.join(self.outdir, filename))
        outdir = os.path.abspath(self.outdir)
        if os.path.commonpath([outdir, fullname]) != outdir:
            return
        self.makedirs(fullname)
        try:
            with open(fullname, "wb") as f:
                f.write(data)
        except Exception as e:
            self.error_log("Failed to save DS_Store file %s" % filename, e=e)

    def ds_store_output_name(self, base_url: str) -> str:
        path = urlparse(base_url).path.strip("/")
        if path:
            return "%s/.DS_Store" % path
        return ".DS_Store"

    def is_html_response(self, data: bytes) -> bool:
        text = data[:256].decode("utf-8", errors="ignore").lstrip().lower()
        return text.startswith("<!doctype") or text.startswith("<html")

    def parse_textual_listing(self, data: bytes) -> list:
        names = []
        text = data.decode("utf-8", errors="ignore")
        for line in text.splitlines():
            match = re.match(
                r"(?i)\s*(?:filename|file|path)\s*[:=]\s*(.+?)\s*$", line
            )
            if match:
                names.append(match.group(1))
        return names

    def is_unsafe_name(self, filename: str) -> bool:
        return bool(
            filename.startswith("/")
            or ".." in filename.split("/")
            or re.search(r'[<>:"|?*\\\x00]', filename)
        )
