# Installation Guide

Supervisor-CAO runs on **WSL2 Ubuntu-24.04** and orchestrates OpenCode
(GLM/Qwen) and Codex CLI providers through a local `cao-server`.

## Prerequisites (inside WSL2)

| Tool | Min version | Install |
|------|-------------|---------|
| WSL2 distro | Ubuntu-24.04 | `wsl --install -d Ubuntu-24.04` |
| Python | 3.10+ (3.12 tested) | `sudo apt install python3 python3-pip` |
| tmux | 3.3+ (3.4 tested) | `sudo apt install tmux` |
| git / gh | recent | `sudo apt install git gh`; `gh auth login` |
| uv | 0.8.x (0.8.6 tested) | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |

Verify: `python3 --version; tmux -V; uv --version; git --version; gh --version`.

## 1. Install CAO (cli-agent-orchestrator)

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

## 2. Install OpenCode CLI

OpenCode 1.18.x is the GLM/Qwen provider. Install per upstream, authenticate
GLM/Qwen; auth lives in `~/.local/share/opencode/auth.json` (or
`$OPENCODE_AUTH_PATH` if shared from Windows). CAO isolates its OpenCode config
at `~/.aws/opencode/`, separate from `~/.config/opencode/`.

```bash
opencode --version     # >= 1.18
opencode models        # lists provider/model IDs (never prints API keys)
```

## 3. Install Codex CLI + ChatGPT auth

Codex CLI 0.146.x is the high-value provider (Plan/Review/Judge). Install per
upstream and complete ChatGPT (Pro) auth.

```bash
codex --version        # >= 0.146
```

If `codex` is not on the WSL PATH, set `CODEX_BIN` to an absolute path
(WSL-side or `/mnt/c/...`). `doctor` honors `CODEX_BIN`.

## 4. Detect models

Generate a desensitized model map (provider/model IDs only, never API keys):

```bash
python3 scripts/detect-models --check    # print only; exit 2 if a role is unconfigured
python3 scripts/detect-models            # writes ~/.config/supervisor-cao/models.local.yaml
```

`models.local.yaml` maps roles (`supervisor_primary`, `glm_executor`,
`qwen_verifier`, `researcher`, `codex`) to detected models. Git-ignored.

## 5. Create project local config

```bash
mkdir -p ~/.config/supervisor-cao/projects
cp config/examples/pandas.example.yaml \
   ~/.config/supervisor-cao/projects/pandas.local.yaml
# Edit: real wsl_repo, windows_repo, remote_validation (ssh_host, containers,
# user, repo_path, conda_env). Never commit this file.
```

## 6. Initialize CAO and verify

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
