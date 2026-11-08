# citevault-py

> 项目 / 发布名是 `citevault-py`；命令行和 Python 包名仍然是 `citevault`。

给 AI 引用做存证，并把它变成 CI 门禁。

AI 写作会产生两条引用风险：引用是编的，或者引用真实但来源后来变了。citevault 只做后者——**引用完整性**：

1. 把「结论 + 原文片段 + 页面快照 + 哈希」绑在一起存下来；
2. 在 CI 里自动复查：页面还在不在、片段还在不在、正文有没有被改；
3. 引用失效就让构建失败，并输出可读报告或 JSON。

不联网上传，不需要 API Key，核心功能零第三方依赖。

## 安装

```bash
pip install -e .
# 需要读 YAML 清单时
pip install -e ".[yaml]"
# 需要抽取 PDF 文本时
pip install -e ".[pdf]"
```

也可以直接跑源码：

```bash
python -m citevault --help
```

## 快速开始

复制一份清单，填入真实引用：

```bash
cp examples/citations.example.yml citations.yml
```

```yaml
citations:
  - claim: "报告显示 2026 年市场规模达到 120 亿美元"
    quote: "the market reached 12 billion dollars in 2026"
    url: "https://example.com/report"
```

批量存证并核查：

```bash
citevault scan citations.yml
```

第一次运行会抓取并保存快照；之后再运行，会重新抓取并比对已有记录。

PDF 需要先安装 `pypdf`，然后用 `--file` 存证（发布到 PyPI 后可改用 `pip install "citevault[pdf]"`）：

```bash
pip install -e ".[pdf]"
citevault add --file ./paper.pdf --claim "..." --quote "..."
```

## 作为 CI 门禁

仓库里提供了复合 Action，内容仓库只需要两步：

```yaml
name: citations
on: [push, pull_request]
jobs:
  verify:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - uses: WHK576/citevault-py@v0.9.1
        with:
          mode: scan
          manifest: citations.yml
          workers: "4"
          block-private: "true"
          delay: "1"
```

Action 默认安装 `[yaml]`；需要 PDF 时把 `extras` 设为 `"[pdf,yaml]"`。

定时复查只需加一个 schedule：
```yaml
on:
  schedule:
    - cron: "0 3 * * 1"
```

完整示例见 [`examples/github-actions/citations.yml`](examples/github-actions/citations.yml)。

**关于存证库的持久化**：Action 默认把证据写在 `.citevault/`。如果希望长期保留审计记录，把 `.citevault/` 从 `.gitignore` 移除并提交进仓库；如果只需要近期门禁，用 `actions/cache` 缓存即可（示例里已经写了）。

## 真实站点回归

仓库里有两套回归：

- 单元测试：`python -m unittest discover -s tests -t . -v`
- 在线回归（默认跳过）：

```bash
CITEVAULT_LIVE=1 python -m unittest tests.test_live_regression -v
```

Windows PowerShell：

```powershell
$env:CITEVAULT_LIVE="1"; python -m unittest tests.test_live_regression -v
```

覆盖 RFC Editor、GNU、IANA、Python.org，以及 robots 拒绝、404、跨主机重定向。它会真实访问公共站点，并遵守 robots / Crawl-delay。另有 [live-regression.yml](.github/workflows/live-regression.yml) 每周一自动跑一次。

## 发布门禁

推送 `v*` tag（例如 `v0.9.1`）会触发 [release.yml](.github/workflows/release.yml)：

1. **unit**：ruff + 单元测试 + coverage ≥ 80%
2. **live**：真实站点回归（调用 `live-regression.yml`）
3. **build**：校验 tag 与 `__version__` 一致，构建 sdist / wheel
4. **release**：创建 GitHub Release

unit 或 live 失败时，不会构建，也不会创建 Release。手动触发 `workflow_dispatch` 只跑检查和构建，不创建 Release。

本地发布前建议跑：

```bash
python -m unittest discover -s tests -t . -v
CITEVAULT_LIVE=1 python -m unittest tests.test_live_regression -v
ruff check citevault tests
```

## 导出证据包

门禁解决的是“发现问题”，证据包解决的是“证明问题”。

```bash
citevault bundle --all -o evidence.cvbundle
```

生成的 `.cvbundle` 是一个普通 zip：

```
bundle.json          # 条目、时间、哈希、核查历史
evidence/<id>.html   # 网页存证时的解码页面（PDF 存证时为 .pdf）
evidence/<id>.txt    # 归一化后的正文，供离线比对
evidence/<id>.raw    # 服务器返回的原始字节（网页存证）
```

任何人都可以在没有你的存证库、也不联网的情况下复核：

```bash
citevault verify-bundle evidence.cvbundle
```

它会重算 `entries_hash` 与 `manifest_hash`，逐条校验快照哈希、原始字节哈希、文本哈希、文本长度和引用片段是否存在。退出码 `0` 表示全部一致，`2` 表示有文件被改动或不一致。

当前格式是 v2；0.8 及更早导出的 v1 证据包仍然可以验证。

**它能证明什么**：这个包在生成之后没有被修改过（包括创建时间等元数据），且每条引用在存证时确实出现在对应正文里，原始字节与快照也一致。

**它不能证明什么**：存证时间本身可信、页面内容确实来自那个 URL。要做到前者，需要把 `entries_hash` 发到外部可信位置（例如 git 提交、公开日志或 GPG 签名）。

## 外部锚定

证据包解决“包内没被改”，锚点解决“包没有被替换”。

```bash
citevault bundle --all -o evidence.cvbundle
citevault anchor evidence.cvbundle --sign
```

会生成两个文件：

```
evidence.cvbundle.anchor.json   # bundle 字节哈希 + entries_hash + 锚定时间
evidence.cvbundle.anchor.json.asc  # GPG 分离签名（仅 --sign 时）
```

验证：

```bash
citevault verify-anchor evidence.cvbundle.anchor.json evidence.cvbundle
```

它会同时检查：

1. 证据包字节与锚点记录的 `bundle_sha256` 是否一致；
2. 包内 `entries_hash` 与锚点记录是否一致；
3. 证据包内部是否仍然通过 `verify-bundle`；
4. 如果存在 `.asc`，GPG 签名是否有效。

退出码 `0` 表示全部通过，`2` 表示不一致或签名无效，`1` 表示文件读不了。

**真正的时间可信来自“你把锚点发到哪里”，不是工具本身**。推荐把 `.anchor.json` 和 `.asc` 一起提交进 git，或者把 `bundle_sha256` 发到公开日志 / OpenTimestamps。GPG 签名只证明“谁签的”，不证明“什么时候签的”。

## 命令

| 命令 | 作用 |
| --- | --- |
| `citevault add --url ... --claim ... --quote ...` | 存证单条引用 |
| `citevault add --file ... --claim ... --quote ...` | 从本地 HTML / PDF 存证 |
| `citevault add --update <id> ...` | 更新已有证据，保留核查历史 |
| `citevault remove <id> --yes` | 删除证据与快照 |
| `citevault scan citations.yml` | 新条目存证，旧条目复查 |
| `citevault list [--json]` | 列出所有存证与最近状态 |
| `citevault check --all [--json]` | 复查全部 |
| `citevault check --all --workers 4` | 不同域名并行，同域名按 `--delay` 串行 |
| `citevault check <id>` | 复查单条 |
| `citevault show <id>` | 查看详情与核查历史 |
| `citevault show <id> --limit 50` | 显示最近 50 次核查 |
| `citevault bundle --all -o evidence.cvbundle` | 导出可独立验证的证据包 |
| `citevault bundle --all -o evidence.cvbundle --force` | 覆盖已存在的输出文件 |
| `citevault verify-bundle evidence.cvbundle` | 离线验证证据包 |
| `citevault anchor evidence.cvbundle --sign` | 生成锚点，可选 GPG 签名 |
| `citevault verify-anchor evidence.anchor.json evidence.cvbundle` | 验证锚点、签名与证据包 |

## 状态含义

| 状态 | 含义 | 是否算问题 |
| --- | --- | --- |
| `ok` | 正文没变，片段还在 | 否 |
| `changed` | 正文变了，片段还在；会附带变化句的 diff | 否 |
| `quote_missing` | 片段已经不在正文里 | 是 |
| `unavailable` | 抓不到页面（404 / 410 等） | 是 |
| `inconclusive` | 403/429/5xx、非 HTML 返回、正文异常变短或出现登录提示 | 是（需人工复核） |
| `error` | 网络错误、超时、文件丢失 | 是 |

退出码：

- `0`：一切正常（包含 `changed`）；
- `2`：出现 `quote_missing`、`unavailable`、`inconclusive` 或 `error`；
- `1`：参数错误、清单格式错误、存证 ID 不存在等。

因此 `scan` / `check` 可以直接作为 CI 步骤。

## 存储结构

```
.citevault/
├── vault.db        # SQLite：存证记录与核查历史
└── snapshots/
    └── <id>.html   # 存证时的原始页面
```

默认目录是当前工作目录下的 `.citevault`，可以用 `--store` 或环境变量 `CITEVAULT_DIR` 指定。

## 设计取舍

- **只做确定性核验**：判断「页面是否可用、片段是否存在、正文是否变化」，不做「这句话是否被原文支撑」的语义判断。
- **区分“坏了”和“没抓到”**：403/429/5xx、非 HTML 返回、正文异常变短，统一报 `inconclusive`，不误报成 `quote_missing`。
- **只抓静态 HTML**：不执行 JavaScript。动态渲染页面请先保存为本地 HTML，或用 `--allow-missing-quote`。
- **只允许 http/https**：拒绝 `file://`、`ftp://` 等 scheme；`--block-private` 会进一步拒绝本地和私有地址，CI 环境建议开启。
- **网络抖动会重试**：超时、429（按 `Retry-After`）和 502/503/504 默认重试。
- **遵守 robots.txt**：按 host 缓存并遵循 Allow / Disallow / Crawl-delay；本地/内网地址跳过。
- **处理压缩响应**：自动解压 gzip/deflate，正文哈希基于解压后内容。
- **条件请求**：存证时保存 `ETag` / `Last-Modified`，核查命中 304 时直接判 `ok`。
- **按域名并发**：`--workers` 让不同域名并行，同一域名仍按 `--delay` 串行，避免把目标站点打爆。
- **跨域重定向会告警**：如果核查时跳到不同主机，报 `inconclusive`，避免域名被接管后静默通过。
- **可更新、可删除**：`add --update` 保留 ID 与核查历史；`remove` 清理数据库与快照。
- **允许缺失不会永久报错**：用 `--allow-missing-quote` 存证的记录，在正文仍缺失时判 `ok` 并注明“存证时已允许”。
- **清单重复条目只警告**：重复项会跳过并显示 `duplicate`，不会让 CI 失败。
- **正文区优先**：依次尝试 `<main>`、`role=main`、`<article>`，最后才退回全文；导航和页脚不会混进比对。
- **`changed` 给出差异**：按句子切分，只显示变化的几行，最多 400 字，`check` / `scan` / `show` 都会打印。
- **精确匹配**：引用片段和正文都会先归一化（全角字符、弯引号、破折号、空白），再判断子串是否存在。
- **`changed` 不算失败**：页面加广告、改时间戳都会触发，只要片段还在，引用就仍然成立。

## 限制

- 扫描版 PDF 没有文本层，无法抽取（不做 OCR）。
- 不支持 Word、EPUB。
- robots.txt 拉取失败时 fail-open（默认允许）；需要严格合规请自行审查。
- `--block-private` 在 DNS 解析与连接之间仍有 rebinding 窗口，是 best-effort。
- 编码检测对“零替换字符但整页乱码”的单字节误判无能为力。
- 不处理登录墙和付费墙，遇到时只能报 `inconclusive`。
- 不做语义支撑判断；片段还在不等于结论一定还成立。
- 证据默认是本地自证，不提供第三方时间戳。

## 路线图

- [ ] MCP server，让 Agent 在生成引用时直接存证
- [ ] Markdown / HTML 文稿内联引用的自动扫描
- [ ] OpenTimestamps 集成

## License

MIT
