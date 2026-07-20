#!/usr/bin/env python3
# -*- coding=utf-8 -*-

"""Run every supported disclosure dumper for one target URL."""

from urllib.parse import urlsplit, urlunsplit

import click

from ..dumper import BaseDumper
from . import dsdumper, gitdumper, hgdumper, idxdumper, svndumper


ADDONS = (
    ("Git", gitdumper.Dumper, ".git/"),
    ("Mercurial", hgdumper.Dumper, ".hg/"),
    ("SVN", svndumper.Dumper, ".svn/"),
    ("DS_Store", dsdumper.Dumper, ".DS_Store"),
    ("Web index", idxdumper.Dumper, ""),
)


def normalize_base_url(url: str) -> str:
    """Return the directory URL that owns possible disclosure artifacts."""
    parsed = urlsplit(url.strip())
    path = parsed.path.rstrip("/")

    # Supplying a legacy artifact URL remains convenient, but auto mode always
    # starts from its containing directory and runs every addon.
    for marker in ("/.git", "/.hg", "/.svn", "/.DS_Store"):
        position = path.find(marker)
        if position >= 0:
            suffix = path[position + len(marker) :]
            if not suffix or suffix.startswith("/"):
                path = path[:position]
                break

    return urlunsplit((parsed.scheme, parsed.netloc, path.rstrip("/"), "", ""))


def addon_url(base_url: str, suffix: str) -> str:
    return "%s/%s" % (base_url.rstrip("/"), suffix)


class Dumper(BaseDumper):
    """Coordinator that executes all dumpers sequentially in one output tree."""

    def __init__(self, url: str, outdir: str, **kwargs):
        super(Dumper, self).__init__(url, outdir, **kwargs)
        self.base_url = normalize_base_url(url)

    async def start(self):
        options = {
            "proxy": self.proxy,
            "force": self.force,
            "debug": self.debug,
        }
        for name, dumper_class, suffix in ADDONS:
            target_url = addon_url(self.base_url, suffix)
            click.secho("=== %s: %s ===" % (name, target_url), fg="cyan")
            try:
                dumper = dumper_class(target_url, self.outdir, **options)
                await dumper.start()
            except Exception as e:
                # Each disclosure type is independent. A malformed artifact
                # must not prevent the remaining checks from running.
                self.error_log("%s dumper failed." % name, e=e)
