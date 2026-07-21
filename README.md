# asana-sync

Pull tasks from an Asana project into a GitHub Projects (v2) board (draft issues by default), or optionally into a GitHub repository as real issues.

## Setup

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

## Usage

Put credentials in `.env` (or export them), then:

```bash
uv run A2G
```

**Project mode** (default): set `GITHUB_PROJECT_OWNER` + `GITHUB_PROJECT_NUMBER`. Creates draft issues on the board; no repository required.

**Repo mode**: set `GITHUB_REPO=owner/repo` to create real issues. You can still set the project vars to also link those issues onto a board. In this mode the sync also writes the GitHub issue number into Asana's `GitHub Issue #` custom field and attaches the issue URL on the task (including for already-synced tasks on later runs).

See the script docstring for the full env var list.

## GitHub Actions

The [Asana sync](.github/workflows/asana-sync.yml) workflow runs on push to `main`/`master` and via **Actions → Asana sync → Run workflow**.

### Repository secrets

Create these under **Settings → Secrets and variables → Actions**:

| Secret | Purpose |
| --- | --- |
| `ASANA_TOKEN` | Asana personal access token |
| `ASANA_SYNC_GITHUB_TOKEN` | GitHub PAT with `repo` / `project` scopes (mapped to `GITHUB_TOKEN` in the job; the name `GITHUB_TOKEN` is reserved by Actions) |
| `ASANA_PROJECT_GID` | Asana project GID |
| `GITHUB_PROJECT_OWNER` | Projects v2 owner (org or user) |
| `GITHUB_PROJECT_NUMBER` | Projects v2 number |
| `GITHUB_REPO` | Optional `owner/repo` for real issues |

Generate the Asana and GitHub PATs manually, then paste them into the secrets above.

### Workflow dispatch stopgap

When running manually, you can fill the same values as workflow inputs instead of (or before) configuring secrets. Prefer secrets for recurring runs — dispatch inputs are visible in the workflow run UI.
