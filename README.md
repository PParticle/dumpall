# Dump all: 多种泄漏形式，一种利用方式

<p align="center">
  <a href="https://github.com/0xHJK/dumpall">
    <img src="https://github.com/0xHJK/dumpall/raw/master/static/dumpall.png" alt="dumpall">
  </a>
  <span>dumpall 是一款信息泄漏/源代码泄漏利用工具</span><br>
<p>

<p align="center">
  <a><img src="https://img.shields.io/pypi/pyversions/dumpall.svg"></a>
  <a href="https://github.com/0xHJK/dumpall/releases">
    <img src="https://img.shields.io/github/v/release/0xHJK/dumpall?include_prereleases">
  </a>
  <a><img src="https://img.shields.io/github/license/0xHJK/dumpall"></a>
</p>

<p align="center">
  <a href="https://github.com/0xHJK/dumpall">https://github.com/0xHJK/dumpall</a>
</p>

<hr>

> ⚠️ **警告：本工具仅用于授权测试，不得用于非法用途，否则后果自负！**
> 
> ⚠️ **WARNING：FOR LEGAL PURPOSES ONLY!**


## 🤘 Features

- 支持多种泄漏情况利用
- Dumpall使用方式简单
- 使用asyncio异步处理速度快

适用于以下场景：

- [x] `.git`源代码泄漏
- [x] `.hg`源代码泄漏
- [x] `.svn`源代码泄漏
- [x] `.DS_Store`信息泄漏
- [x] 目录列出信息泄漏

TODO:

- [ ] 支持更多利用方式
- [ ] 优化大文件下载
- [ ] 增强绕过功能

项目地址：<https://github.com/0xHJK/dumpall>

> 当前版本要求 Python 3.9+


## 🚀 QuickStart

```bash
# 源码目录使用 uv 安装依赖
uv sync
# 查看版本
uv run dumpall --version
```

```bash
# 也可以使用 pip 安装
python3 -m pip install .
dumpall --version
```

## 💫 Usage

```bash
# 自动检查并下载 .git、.hg、.svn、.DS_Store 和 Web 目录索引
uv run dumpall -u <url> [-o <outdir>]

# 示例
uv run dumpall -u http://example.com/
```

给定一个基础 URL 后，dumpall 会依次运行 Git、Mercurial、SVN、DS_Store 和 Web 目录索引下载器，
任一类型不存在或解析失败都不会阻止后续检查。
为兼容旧用法，传入 `/.git/`、`/.hg/`、`/.svn/` 或 `/.DS_Store` URL 时也会先回到所在目录，再执行全部检查。
默认输出为精简模式，仅显示模块状态、汇总和关键恢复结果；如需查看每个请求/文件的详细日志，可加 `-d`。
存在泄露的模块会高亮显示，例如 `[hg].................. recovered flag.php`。

Git 泄露会同时保存 `.git/HEAD`、`refs`、`logs`、`index` 及可达 commit/tree/blob 对象，
因此输出目录可直接用 `git log --all`、`git show <commit>:<file>` 查看仓库历史。
Mercurial 泄露会保存 `.hg/store` revlog 文件，并从 `data/*.i` / `data/*.d` 自动恢复最新源码文件。

帮助

```bash
$ uv run dumpall --help
Usage: dumpall [OPTIONS]

  信息泄漏利用工具，自动检查.git/.hg/.svn/.DS_Store和目录索引

  Example: dumpall -u http://example.com/

Options:
  --version          Show the version and exit.
  -u, --url TEXT     指定目标URL，自动检查.git/.hg/.svn/.DS_Store和目录索引
  -o, --outdir TEXT  指定下载目录，默认为dist
  -p, --proxy TEXT   指定代理 scheme://[user:pass@]hostname:port
  -f, --force        强制下载（可能会有蜜罐风险）
  -d, --debug        调试模式 输出更多日志
  --help             Show this message and exit.
```

`.git`源代码泄漏利用

![0xHJK dumpall gitdumper](https://github.com/0xHJK/dumpall/raw/master/static/gitdumper.png)

`.svn`源代码泄漏利用

![0xHJK dumpall svndumper](https://github.com/0xHJK/dumpall/raw/master/static/svndumper.png)

`.DS_Store`信息泄漏利用

![0xHJK dumpall dsdumper](https://github.com/0xHJK/dumpall/raw/master/static/dsdumper.png)

## 🙋 FAQ

1. `OSError(24, 'Too many open files'))`

手动修改系统打开文件最大数量限制，如 `ulimit -n 65535`

2. 旧版本SVN无法利用

先用idxdumper凑合，等有空补充

## 📜 History

- 2022-05-09 v0.4.0
  - 优化基础功能，修复BUG
  - 增加调试模式
  - 优化多任务调度
  - 支持代理
  - 支持随机UserAgent
- 2022-03-01 v0.3.2
  - 修复URL编码问题
- 2021-08-09 v0.3.1
  - 修复任意位置存储漏洞、增加蜜罐警告
- 2020-05-22 v0.3.0
  - 完成目录列出信息泄漏利用功能
- 2019-10-27 v0.2.0
  - 优化下载方法
  - 完成`.DS_Store`信息泄漏利用功能
- 2019-10-24 v0.1.0
  - 项目架构优化
  - 完成`.svn`源代码泄漏利用功能
- 2019-10-23
  - 完成`.git`源代码泄漏利用功能
- 2019-10-19 项目启动

## 🤝 Contributions

本项目参考或使用了以下项目，在此感谢相关开发者

- https://github.com/lijiejie/GitHack
- https://github.com/admintony/svnExploit
- https://github.com/sbp/gin
- https://github.com/gehaxelt/Python-dsstore
- https://github.com/aio-libs/aiohttp
- https://github.com/jreese/aiomultiprocess
- https://github.com/pallets/click

感谢以下开发者的贡献

- @k0ngfei
- @nian-hua
- @fabaff

如有意愿参与项目开发，请遵循以下规范

- 使用下划线命名法命名
- 使用 <https://github.com/psf/black> 做代码格式化

## 📄 License

[MIT License](https://github.com/0xHJK/TotalPass/blob/master/LICENSE)

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=0xHJK/dumpall&type=Date)](https://star-history.com/#0xHJK/dumpall&Date)
