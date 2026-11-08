# 贡献指南

感谢你愿意改进 citevault-py。这个项目很小，保持简单比增加功能更重要。

## 开始之前

- 先搜索现有 Issue 和 Discussions，避免重复。
- 较大的改动先开 Issue 说明动机和范围，再写代码。
- 安全问题不要公开开 Issue，见下方「安全披露」。

## 开发环境

要求 Python >= 3.10。

```bash
git clone https://github.com/WHK576/citevault-py.git
cd citevault-py
python -m venv .venv
```

Windows：

```powershell
.venv\Scripts\activate
pip install -e ".[pdf,yaml,dev]"
```

macOS / Linux：

```bash
source .venv/bin/activate
pip install -e ".[pdf,yaml,dev]"
```

## 提交前检查

```bash
ruff check citevault tests
python -m unittest discover -s tests -t .
```

- 单元测试必须全部通过。
- 修改行为时补充测试。
- 覆盖率目标 80%，不要明显拉低。

## 真实站点回归

默认跳过，避免普通 PR 依赖公网。需要时手动运行：

Windows PowerShell：

```powershell
$env:CITEVAULT_LIVE = "1"
python -m unittest tests.test_live_regression -v
```

macOS / Linux：

```bash
CITEVAULT_LIVE=1 python -m unittest tests.test_live_regression -v
```

只在验证抓取、robots、重定向、304 时运行；不要在单元测试里新增对公共站点的依赖。

## 代码约定

- 核心保持零第三方依赖；PDF / YAML 只能作为 optional extra。
- 不做「引用是否支撑结论」的语义判断，这是项目的明确边界。
- 抓取必须遵守 robots.txt 与 Crawl-delay，不要绕过 `fetch.py` 直接 `urlopen`。
- 文件写入尽量保持原子性，错误不要静默吞掉。
- 新状态优先复用 `ok / changed / quote_missing / inconclusive / unavailable / error`。

## Pull Request

1. Fork 仓库，从 `main` 开分支。
2. 一个 PR 只做一件事，避免混入无关格式化。
3. PR 描述写清动机、改动和验证方式。
4. CI 必须通过；live 回归由维护者决定是否触发。
5. 收到 review 后尽量用新提交回应，不要 force-push 覆盖讨论。

## Issue

- Bug：使用 Bug report 模板，附版本、Python 版本、复现步骤和相关输出。
- 功能建议：使用 Feature request 模板，说明使用场景和现有替代方案。
- 不要贴真实密钥、内网 URL 或未脱敏数据。

## 安全披露

不要公开开 Issue。请使用 GitHub 的私密漏洞报告：

https://github.com/WHK576/citevault-py/security/advisories/new

## 许可证

贡献即表示同意以 MIT 许可证发布。
