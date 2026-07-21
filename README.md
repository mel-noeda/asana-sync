# asana-sync

Two-way helpers between Asana and GitHub:

- **A2G** — pull Asana tasks into GitHub (Projects draft issues and/or repo issues)
- **G2A** — push a GitHub issue/PR update into its linked Asana task (including board column)

## Setup

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

## Asana → GitHub (`uv run A2G`)

Put credentials in `.env` (or export them), then:

```bash
uv run A2G
```

**Project mode** (default): set `GITHUB_PROJECT_OWNER` + `GITHUB_PROJECT_NUMBER`. Creates draft issues on the board; no repository required.

**Repo mode**: set `GITHUB_REPO=owner/repo` to create real issues. You can still set the project vars to also link those issues onto a board. In this mode the sync also writes the GitHub issue number into Asana's `GitHub Issue #` custom field and attaches the issue URL on the task (including for already-synced tasks on later runs).

## GitHub → Asana (`uv run G2A`)

Fast path for a single issue/PR:

```bash
uv run G2A 42
uv run G2A --issue 42
```

Reconcile **all** board columns (Status → Asana sections):

```bash
uv run G2A --all-project-items --columns-only
```

Requires `ASANA_TOKEN`, `ASANA_PROJECT_GID`, `GITHUB_TOKEN`. Single-issue mode also needs `GITHUB_REPO`. Column sync needs `GITHUB_PROJECT_OWNER` + `GITHUB_PROJECT_NUMBER`.

The Asana task is resolved from:

1. `<!-- asana-task-gid:... -->` marker in the issue body, or
2. a task whose `GitHub Issue #` custom field matches the issue number

Updates:

| GitHub | Asana |
| --- | --- |
| title | task name |
| body | notes |
| open/closed (+ Status `Done`) | completed |
| labels | Labels / Issue Type / Priority / Story Points (`sp:3`) |
| Projects **Status** | project section (column) |

Status names are matched flexibly to Asana sections, e.g. `Ready` → `Ready / Sprint`, `In progress` → `In Progress`.

## GitHub Actions

### Asana → GitHub

[asana-sync.yml](.github/workflows/asana-sync.yml) — scheduled / push / manual full sync (`uv run A2G`).

### GitHub → Asana

[github-to-asana.yml](.github/workflows/github-to-asana.yml):

- issue / pull_request updates → full sync for that item (includes Status column)
- **schedule every 2 minutes** → `--all-project-items --columns-only` (covers board column moves)
- `repository_dispatch` type `github_project_item` → optional near-real-time bridge
- `workflow_dispatch` → test a single issue or reconcile all columns

GitHub Actions cannot natively trigger on `projects_v2_item`. The 2-minute schedule is the built-in stopgap so column moves show up in Asana quickly. For faster delivery, forward an org/user `projects_v2_item` webhook to:

```bash
gh api repos/<owner>/<repo>/dispatches -f event_type=github_project_item \
  -f client_payload[issue_number]=42 \
  -f client_payload[github_repo]=owner/repo \
  -f client_payload[status]='In progress'
```

### Repository secrets

Create these under **Settings → Secrets and variables → Actions**:

| Secret | Purpose |
| --- | --- |
| `ASANA_TOKEN` | Asana personal access token |
| `ASANA_SYNC_GITHUB_TOKEN` | GitHub PAT with `repo` / `project` scopes (mapped to `GITHUB_TOKEN` in jobs; the name `GITHUB_TOKEN` is reserved by Actions) |
| `ASANA_PROJECT_GID` | Asana project GID |
| `GITHUB_PROJECT_OWNER` | Projects v2 owner (org or user) |
| `GITHUB_PROJECT_NUMBER` | Projects v2 number |
| `GITHUB_REPO` | `owner/repo` for real issues (G2A defaults to the current repository if unset) |

Generate the Asana and GitHub PATs manually, then paste them into the secrets above.

### Workflow dispatch stopgap

When running manually, you can fill tokens/config as workflow inputs instead of (or before) configuring secrets. Prefer secrets for recurring runs — dispatch inputs are visible in the workflow run UI.
