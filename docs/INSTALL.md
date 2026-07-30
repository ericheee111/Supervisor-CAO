[English](#installation-guide) | [简体中文](#安装指南)

# Installation Guide

Supervisor-CAO runs on **WSL2 Ubuntu-24.04** and orchestrates OpenCode
(GLM/Qwen) and Codex CLI providers through a local `cao-server`.

## Prerequisites (inside WSL2)

| Tool | Min version | Install |
|------|-------------|---------|
| WSL2 distro | Ubuntu-24.04 | `wsl --install -d Ubuntu-24.04` |
| Python | 3.10+ (3.12 tested) | `sudo apt install python3` (system Python is PEP 668-managed — never `pip install` against it) |
| tmux | 3.3+ (3.4 tested) | `sudo apt install tmux` |
| git / gh | recent | `sudo apt install git gh`; `gh auth login` |
| uv | 0.8.x (0.8.6 tested) | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |

Verify: `python3 --version; tmux -V; uv --version; git --version; gh --version`.

> Ubuntu's system Python is PEP 668-managed. Never run bare `pip install`
> against it and never use `--break-system-packages` — install the workspace
> into an isolated `.venv` (step 1) and the upstream CAO tool via `uv tool`
> (step 2).

## 1. Install Supervisor-CAO into an isolated venv

From the workspace root (where `pyproject.toml` lives), create an isolated
virtualenv and install the workspace **editable with the `dev` extra**. This
provides the `supervisor-cao` and `supervisor-cao-policy-mcp` entry points
from this checkout — not a stale global install.

```bash
uv venv .venv                                    # or: python3 -m venv .venv
uv pip install -e ".[dev]"                       # or: .venv/bin/pip install -e ".[dev]"
source .venv/bin/activate                        # activate before running any supervisor-cao command
```

Activate the venv in every shell that will run `supervisor-cao`. Confirm both
binaries resolve to this workspace's `.venv`, not a global install:

```bash
which supervisor-cao              # must print .../Supervisor-CAO/.venv/bin/supervisor-cao
which supervisor-cao-policy-mcp   # must print .../Supervisor-CAO/.venv/bin/supervisor-cao-policy-mcp
```

## 2. Install CAO (cli-agent-orchestrator)

CAO is pinned to a tested commit in `config/cao_pinned.sha`:
`4cc40b182d259f8a370ec3f70fb00a0d67b7844d` (awslabs/cli-agent-orchestrator@main, v2.3.0).

```bash
uv tool install git+https://github.com/awslabs/cli-agent-orchestrator.git@main
cao --version
# Pin to the exact tested SHA:
uv tool install --force --reinstall \
  git+https://github.com/awslabs/cli-agent-orchestrator.git@4cc40b182d259f8a370ec3f70fb00a0d67b7844d
```

`supervisor-cao doctor` reports the pinned SHA. Upgrades are explicit
(`supervisor-cao upgrade`, runs a regression suite first).

## 3. Install OpenCode CLI

OpenCode 1.18.x is the GLM/Qwen provider. Install per upstream, authenticate
GLM/Qwen; auth lives in `~/.local/share/opencode/auth.json` (or
`$OPENCODE_AUTH_PATH` if shared from Windows). CAO isolates its OpenCode config
at `~/.aws/opencode/`, separate from `~/.config/opencode/`.

```bash
opencode --version     # >= 1.18
opencode models        # lists provider/model IDs (never prints API keys)
```

## 4. Install Codex CLI + ChatGPT auth

Codex CLI 0.146.x is the high-value provider (Plan/Review/Judge). Install per
upstream and complete ChatGPT (Pro) auth.

```bash
codex --version        # >= 0.146
```

If `codex` is not on the WSL PATH, set `CODEX_BIN` to an absolute path
(WSL-side or `/mnt/c/...`). `doctor` honors `CODEX_BIN`.

## 5. Detect models

Generate a desensitized model map (provider/model IDs only, never API keys):

```bash
python3 scripts/detect-models --check    # print only; exit 2 if a role is unconfigured
python3 scripts/detect-models            # writes ~/.config/supervisor-cao/models.local.yaml
```

`models.local.yaml` maps roles (`supervisor_primary`, `glm_executor`,
`qwen_verifier`, `researcher`, `codex`) to detected models. Git-ignored.

## 6. Create project local config

`config/examples/demo-project.example.yaml` is a fully fictional, public
template — real hosts, container names, usernames, and paths never go in it.

```bash
mkdir -p ~/.config/supervisor-cao/projects
cp config/examples/demo-project.example.yaml \
   ~/.config/supervisor-cao/projects/demo-project.local.yaml
# Edit the local copy: real wsl_repo, windows_repo, remote_validation
# (ssh_host, containers, user, repo_path, env). Never commit this file.
```

## 7. Initialize CAO and verify

```bash
cao init                              # one-time CAO workspace init
supervisor-cao up                     # starts cao-server (HTTP+UI on :9889)
curl -s http://127.0.0.1:9889/health  # expect 200 / "ok"
supervisor-cao doctor                 # all marks green: CAO 2.3.0, cao-server
                                      # 200, OpenCode 1.18.8, Codex 0.146.0,
                                      # uv 0.8.6, tmux 3.4, pinned SHA matching
```

### CAO Web UI (optional)

`cao-server` serves a browser dashboard at `http://localhost:9889` for managing
sessions, spawning agents, viewing live terminals, and inspecting agent-to-agent
messages. The pre-built frontend bundle (`web_ui/`) is included in PyPI wheels
but **not** in git-source or offline installs. If `http://localhost:9889`
returns `{"detail":"Not Found"}`, build it from source (requires Node.js 18+):

```bash
cd <cao-source>/web/
npm install && npm run build          # outputs to ../src/cli_agent_orchestrator/web_ui/
CAO_INST=$(find ~/.local/share/uv -type d -name cli_agent_orchestrator -path "*site-packages*" | head -1)
cp -r ../src/cli_agent_orchestrator/web_ui "$CAO_INST/web_ui"
supervisor-cao down && supervisor-cao up   # restart to serve the UI
```

Then open `http://localhost:9889` in a browser (WSL2 mirrored networking shares
localhost with Windows).

## Offline / restricted-network install

If WSL2 has no direct internet (e.g. a fake-ip VPN hijacks DNS), pre-build a
Linux wheelhouse on a connected machine and install offline.

On a connected Linux box (matching the target platform):

```bash
uv pip compile --python-platform x86_64-unknown-linux-gnu \
  "git+https://github.com/awslabs/cli-agent-orchestrator.git@4cc40b182d259f8a370ec3f70fb00a0d67b7844d" -o cao-reqs.txt
uv pip download --python-platform x86_64-unknown-linux-gnu -r cao-reqs.txt -d ./wheelhouse
# Transfer ./wheelhouse + cao-reqs.txt to the target WSL2 host.
```

On the offline WSL2 host:

```bash
uv tool install --offline --find-links ./wheelhouse --from ./wheelhouse cli-agent-orchestrator
```

For DNS hijack details see `docs/TROUBLESHOOTING.md` (DoH + `/etc/hosts`).

## Next steps

`docs/USER_GUIDE.md` — daily workflow. `docs/ADD_PROJECT.md` — adding a
project. `docs/SECURITY.md` — what may never be committed.

---

# 安装指南

Supervisor-CAO 运行在 **WSL2 Ubuntu-24.04** 之上，通过本地 `cao-server`
编排 OpenCode（GLM/Qwen）和 Codex CLI 提供方。

## 前置条件（在 WSL2 内）

| 工具 | 最低版本 | 安装方式 |
|------|----------|----------|
| WSL2 发行版 | Ubuntu-24.04 | `wsl --install -d Ubuntu-24.04` |
| Python | 3.10+（已测试 3.12） | `sudo apt install python3`（系统 Python 受 PEP 668 管理 —— 绝不对它执行 `pip install`） |
| tmux | 3.3+（已测试 3.4） | `sudo apt install tmux` |
| git / gh | 较新版本 | `sudo apt install git gh`; `gh auth login` |
| uv | 0.8.x（已测试 0.8.6） | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |

验证：`python3 --version; tmux -V; uv --version; git --version; gh --version`。

> Ubuntu 的系统 Python 受 PEP 668 管理。绝不对它执行裸 `pip install`，
> 也绝不使用 `--break-system-packages` —— 工作区安装进隔离的 `.venv`
> （第 1 步），上游 CAO 工具通过 `uv tool` 安装（第 2 步）。

## 1. 将 Supervisor-CAO 安装到隔离 venv

在工作区根目录（`pyproject.toml` 所在处）创建隔离虚拟环境，并以**带 `dev`
extra 的可编辑模式**安装工作区。这样 `supervisor-cao` 与
`supervisor-cao-policy-mcp` 入口点来自本次 checkout —— 而非过期的全局安装。

```bash
uv venv .venv                                    # 或：python3 -m venv .venv
uv pip install -e ".[dev]"                       # 或：.venv/bin/pip install -e ".[dev]"
source .venv/bin/activate                        # 运行任何 supervisor-cao 命令前先激活
```

在每个要运行 `supervisor-cao` 的 shell 中激活该 venv。确认两个二进制都解析
到本工作区的 `.venv`，而非全局安装：

```bash
which supervisor-cao              # 必须输出 .../Supervisor-CAO/.venv/bin/supervisor-cao
which supervisor-cao-policy-mcp   # 必须输出 .../Supervisor-CAO/.venv/bin/supervisor-cao-policy-mcp
```

## 2. 安装 CAO (cli-agent-orchestrator)

CAO 在 `config/cao_pinned.sha` 中固定到一个已测试的 commit：
`4cc40b182d259f8a370ec3f70fb00a0d67b7844d`（awslabs/cli-agent-orchestrator@main，
v2.3.0）。

```bash
uv tool install git+https://github.com/awslabs/cli-agent-orchestrator.git@main
cao --version
# 固定到确切已测试的 SHA：
uv tool install --force --reinstall \
  git+https://github.com/awslabs/cli-agent-orchestrator.git@4cc40b182d259f8a370ec3f70fb00a0d67b7844d
```

`supervisor-cao doctor` 会报告固定的 SHA。升级是显式的
（`supervisor-cao upgrade`，会先运行回归测试套件）。

## 3. 安装 OpenCode CLI

OpenCode 1.18.x 是 GLM/Qwen 提供方。按上游说明安装，并完成
GLM/Qwen 认证；认证信息存放在 `~/.local/share/opencode/auth.json`
（或者如果从 Windows 共享，则存放在 `$OPENCODE_AUTH_PATH`）。CAO 将其
OpenCode 配置隔离在 `~/.aws/opencode/`，与 `~/.config/opencode/` 分开。

```bash
opencode --version     # >= 1.18
opencode models        # 列出 provider/model ID（绝不打印 API key）
```

## 4. 安装 Codex CLI + ChatGPT 认证

Codex CLI 0.146.x 是高价值提供方（Plan/Review/Judge）。按上游说明安装
并完成 ChatGPT (Pro) 认证。

```bash
codex --version        # >= 0.146
```

如果 `codex` 不在 WSL PATH 上，请将 `CODEX_BIN` 设置为绝对路径
（WSL 侧或 `/mnt/c/...`）。`doctor` 会遵循 `CODEX_BIN`。

## 5. 检测模型

生成一个脱敏的模型映射（仅 provider/model ID，绝不包含 API key）：

```bash
python3 scripts/detect-models --check    # 仅打印；如果某个角色未配置则退出码为 2
python3 scripts/detect-models            # 写入 ~/.config/supervisor-cao/models.local.yaml
```

`models.local.yaml` 将角色（`supervisor_primary`、`glm_executor`、
`qwen_verifier`、`researcher`、`codex`）映射到检测到的模型。已 git-ignore。

## 6. 创建项目本地配置

`config/examples/demo-project.example.yaml` 是一个完全虚构的、公开的模板
— 真实的主机名、容器名、用户名和路径绝不放入其中。

```bash
mkdir -p ~/.config/supervisor-cao/projects
cp config/examples/demo-project.example.yaml \
   ~/.config/supervisor-cao/projects/demo-project.local.yaml
# 编辑本地副本：真实的 wsl_repo、windows_repo、remote_validation
#（ssh_host、containers、user、repo_path、env）。切勿提交此文件。
```

## 7. 初始化 CAO 并验证

```bash
cao init                              # 一次性 CAO 工作区初始化
supervisor-cao up                     # 启动 cao-server（HTTP+UI 在 :9889）
curl -s http://127.0.0.1:9889/health  # 期望 200 / "ok"
supervisor-cao doctor                 # 所有标记为绿色：CAO 2.3.0、cao-server
                                      # 200、OpenCode 1.18.8、Codex 0.146.0、
                                      # uv 0.8.6、tmux 3.4、固定 SHA 匹配
```

### CAO Web UI（可选）

`cao-server` 在 `http://localhost:9889` 提供一个浏览器仪表板，用于管理
会话、生成 agent、查看实时终端以及检查 agent 之间的消息。预构建的前端
bundle（`web_ui/`）包含在 PyPI wheel 中，但**不**包含在 git 源码或离线
安装中。如果 `http://localhost:9889` 返回 `{"detail":"Not Found"}`，请
从源码构建（需要 Node.js 18+）：

```bash
cd <cao-source>/web/
npm install && npm run build          # 输出到 ../src/cli_agent_orchestrator/web_ui/
CAO_INST=$(find ~/.local/share/uv -type d -name cli_agent_orchestrator -path "*site-packages*" | head -1)
cp -r ../src/cli_agent_orchestrator/web_ui "$CAO_INST/web_ui"
supervisor-cao down && supervisor-cao up   # 重启以提供 UI
```

然后在浏览器中打开 `http://localhost:9889`（WSL2 镜像网络与 Windows 共享
localhost）。

## 离线 / 受限网络安装

如果 WSL2 没有直接互联网（例如 fake-ip VPN 劫持了 DNS），请在有网络的
机器上预构建 Linux wheelhouse，然后离线安装。

在一台联网的 Linux 机器上（与目标平台匹配）：

```bash
uv pip compile --python-platform x86_64-unknown-linux-gnu \
  "git+https://github.com/awslabs/cli-agent-orchestrator.git@4cc40b182d259f8a370ec3f70fb00a0d67b7844d" -o cao-reqs.txt
uv pip download --python-platform x86_64-unknown-linux-gnu -r cao-reqs.txt -d ./wheelhouse
# 将 ./wheelhouse + cao-reqs.txt 传输到目标 WSL2 主机。
```

在离线的 WSL2 主机上：

```bash
uv tool install --offline --find-links ./wheelhouse --from ./wheelhouse cli-agent-orchestrator
```

有关 DNS 劫持的详情，请参阅 `docs/TROUBLESHOOTING.md`（DoH +
`/etc/hosts`）。

## 下一步

`docs/USER_GUIDE.md` — 日常工作流。`docs/ADD_PROJECT.md` — 添加项目。
`docs/SECURITY.md` — 绝不可提交的内容。
