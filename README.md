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
