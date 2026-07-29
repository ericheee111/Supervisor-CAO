# Installation Guide

Supervisor-CAO runs on **WSL2 Ubuntu-24.04** and orchestrates OpenCode (GLM/Qwen)
and Codex CLI providers through a local `cao-server`. This guide installs every
dependency from scratch and verifies it with `supervisor-cao doctor`.

## Prerequisites

Run everything inside WSL2 (`wsl -d Ubuntu-24.04` from Windows).

| Tool | Minimum version | Install |
|------|-----------------|---------|
| WSL2 distro | Ubuntu-24.04 | `wsl --install -d Ubuntu-24.04` |
| Python | 3.10+ (3.12 tested) | `sudo apt install python3 python3-pip` |
| tmux | 3.3+ (3.4 tested) | `sudo apt install tmux` |
| git | any recent | `sudo apt install git` |
| gh (GitHub CLI) | any recent | `sudo apt install gh` then `gh auth login` |
| uv | 0.8.x (0.8.6 tested) | `curl -LsSf https://astral.sh/uv/install.sh | sh` |

Verify:

```bash
python3 --version      # >= 3.10
tmux -V                # >= 3.3
uv --version           # 0.8.x
git --version
gh --version
```

## 1. Install CAO (cli-agent-orchestrator)

CAO is pinned to a tested commit recorded in `config/cao_pinned.sha`:

```
4cc40b182d259f8a370ec3f70fb00a0d67b7844d   (awslabs/cli-agent-orchestrator@main, v2.3.0)
```

Install CAO with `uv`:

```bash
uv tool install git+https://github.com/awslabs/cli-agent-orchestrator.git@main
cao --version          # confirm it is on PATH
```

After install, pin to the tested SHA by checking out that commit in the source
clone CAO used, or by reinstalling at the exact commit:

```bash
uv tool install --force --reinstall \
  git+https://github.com/awslabs/cli-agent-orchestrator.git@4cc40b182d259f8a370ec3f70fb00a0d67b7844d
```

The pinned SHA lives in `config/cao_pinned.sha`. `supervisor-cao doctor` reports
it. Upgrades are explicit (`supervisor-cao upgrade`) and run a regression suite
first.

## 2. Install OpenCode CLI

OpenCode 1.18.x is the GLM/Qwen provider. Install per the upstream instructions
for your platform, then authenticate providers (GLM, Qwen). Auth is stored in
`~/.local/share/opencode/auth.json` (or a path you set via `OPENCODE_AUTH_PATH`).

```bash
opencode --version     # >= 1.18
opencode models        # lists provider/model IDs (never prints API keys)
```

CAO isolates its OpenCode config at `~/.aws/opencode/`, separate from your
personal `~/.config/opencode/`.

## 3. Install Codex CLI + ChatGPT auth

Codex CLI 0.146.x is the high-value provider (Plan/Review/Judge). Install per
upstream instructions and complete ChatGPT (Pro) auth interactively.

```bash
codex --version        # >= 0.146
```

If `codex` is not on the WSL PATH (e.g. installed on the Windows side), set:

```bash
export CODEX_BIN="/mnt/c/path/to/codex.exe"   # or a WSL-side absolute path
```

`supervisor-cao doctor` honors `CODEX_BIN` when probing Codex.

## 4. Detect models

Generate a desensitized model map (provider/model IDs only, never API keys):

```bash
cd /path/to/Supervisor-CAO
python3 scripts/detect-models --check        # print only, exit 2 if a role is unconfigured
python3 scripts/detect-models                # writes ~/.config/supervisor-cao/models.local.yaml
```

`models.local.yaml` maps roles (`supervisor_primary`, `glm_executor`,
`qwen_verifier`, `researcher`, `codex`) to detected models. It is git-ignored.

## 5. Create project local config

For each project, copy the sanitized example and fill in private values:

```bash
mkdir -p ~/.config/supervisor-cao/projects
cp config/examples/pandas.example.yaml \
   ~/.config/supervisor-cao/projects/pandas.local.yaml
# Edit pandas.local.yaml: real wsl_repo, windows_repo, remote_validation
# (ssh_host, containers, user, repo_path, conda_env). Never commit this file.
```

## 6. Initialize CAO and check the server

```bash
cao init                                   # one-time CAO workspace init
supervisor-cao up                          # starts cao-server (HTTP+UI on :9889)
curl -s http://127.0.0.1:9889/health       # expect 200 / "ok"
```

## 7. Run doctor

```bash
supervisor-cao doctor
```

Expected output (all marks green):

```
  ✓ CAO                    2.3.0
  ✓ cao-server             200
  ✓ OpenCode               1.18.8
  ✓ Codex CLI              0.146.0
  ✓ uv                     0.8.6
  ✓ tmux                   3.4
  ✓ projects               pandas
  ✓ CAO pinned SHA         4cc40b182d259f8a370ec3f70fb00a0d67b7844d
```

## Offline / restricted-network install

If WSL2 has no direct internet (e.g. a fake-ip VPN hijacks DNS), pre-build a
Linux wheelhouse on a connected machine and install offline.

On a connected Linux box (matching the target platform):

```bash
# Resolve CAO + deps into a requirements set, then download linux wheels
uv pip compile --python-platform x86_64-unknown-linux-gnu \
  "git+https://github.com/awslabs/cli-agent-orchestrator.git@4cc40b182d259f8a370ec3f70fb00a0d67b7844d" \
  -o cao-reqs.txt
uv pip download --python-platform x86_64-unknown-linux-gnu \
  -r cao-reqs.txt -d ./wheelhouse
# Transfer ./wheelhouse + cao-reqs.txt to the target WSL2 host.
```

On the offline WSL2 host:

```bash
uv tool install --offline --find-links ./wheelhouse \
  --from ./wheelhouse cli-agent-orchestrator
```

For DNS hijack issues specifically, see `docs/TROUBLESHOOTING.md` (DoH +
`/etc/hosts`, or the offline wheelhouse above).

## Next steps

- `docs/USER_GUIDE.md` — daily workflow.
- `docs/ADD_PROJECT.md` — adding a new project.
- `docs/SECURITY.md` — what may never be committed.
