# asana-sync

Two-way sync between Asana and GitHub:

- **A2G** — pull Asana tasks into GitHub (Projects draft issues and/or repository issues)
- **G2A** — push a GitHub issue or pull request into its linked Asana task, including the board column

Use this repository as a GitHub Action. Other repositories add a short workflow and secrets. They do not copy Python scripts.

## Use in another repository

1. Create the secrets below under **Settings → Secrets and variables → Actions**.
2. Add a workflow that calls `mel-noeda/asana-sync@v1`.
3. Pin `@v1` so you receive compatible patches, or pin a full tag such as `@v1.0.0`.

The action installs Python 3.14 on the runner through `uv`. You do not need Python in the consuming repository.

Copy-paste starters live in [`examples/`](examples/).

### Secrets

| Secret | Purpose |
| --- | --- |
| `ASANA_TOKEN` | Asana personal access token |
| `ASANA_SYNC_GITHUB_TOKEN` | GitHub PAT with `repo` and/or `project` scopes |
| `ASANA_PROJECT_GID` | Asana project GID |
| `GITHUB_PROJECT_OWNER` | Projects v2 owner (org or user) |
| `GITHUB_PROJECT_NUMBER` | Projects v2 number |
| `GITHUB_REPO` | `owner/repo` for real issues (optional when the workflow sets `github_repo`) |

Store tokens as secrets only. Do not pass them as `workflow_dispatch` inputs — those values appear in the workflow run UI.

The built-in job token is named `GITHUB_TOKEN` and is reserved. It also lacks the Projects access these jobs need. Create a PAT and store it as `ASANA_SYNC_GITHUB_TOKEN`.

Token scopes and local setup are in [USAGE.md](USAGE.md).

### Asana → GitHub

```yaml
# .github/workflows/asana-to-github.yml
name: Asana → GitHub
on:
  schedule:
    - cron: "0 * * * *"
  workflow_dispatch:
jobs:
  sync:
    runs-on: ubuntu-latest
    steps:
      - uses: mel-noeda/asana-sync@v1
        with:
          mode: a2g
          asana_token: ${{ secrets.ASANA_TOKEN }}
          github_token: ${{ secrets.ASANA_SYNC_GITHUB_TOKEN }}
          asana_project_gid: ${{ secrets.ASANA_PROJECT_GID }}
          github_repo: ${{ github.repository }}
          github_project_owner: ${{ secrets.GITHUB_PROJECT_OWNER }}
          github_project_number: ${{ secrets.GITHUB_PROJECT_NUMBER }}
```

### GitHub → Asana

Issue and pull request events sync that item. A two-minute schedule reconciles Projects **Status** into Asana sections. Actions cannot trigger on `projects_v2_item`; the schedule is the built-in stopgap for column moves.

The full starter, including mode resolution, is [examples/github-to-asana.yml](examples/github-to-asana.yml).

For fewer lines in the consuming repository, call the reusable workflow instead:

```yaml
# .github/workflows/github-to-asana.yml
name: GitHub → Asana
on:
  issues:
    types: [opened, edited, closed, reopened, labeled, unlabeled, assigned, unassigned]
  pull_request:
    types: [opened, edited, closed, reopened, labeled, unlabeled, synchronize, ready_for_review, converted_to_draft]
  schedule:
    - cron: "*/2 * * * *"
  repository_dispatch:
    types: [github_project_item]
  workflow_dispatch:
jobs:
  sync:
    uses: mel-noeda/asana-sync/.github/workflows/reusable-github-to-asana.yml@v1
    secrets: inherit
```

Reusable workflows do not appear in the GitHub Marketplace catalog. The composite action does.

### Near-real-time column moves

Forward an organization or user `projects_v2_item` webhook as `repository_dispatch` with type `github_project_item`:

```bash
gh api repos/<owner>/<repo>/dispatches \
  -f event_type=github_project_item \
  -f client_payload[issue_number]=42 \
  -f client_payload[github_repo]=owner/repo \
  -f client_payload[status]='In progress'
```

### Action inputs

| Input | Required | Notes |
| --- | --- | --- |
| `mode` | yes | `a2g` or `g2a` |
| `asana_token` | yes | From `ASANA_TOKEN` |
| `github_token` | yes | From `ASANA_SYNC_GITHUB_TOKEN` |
| `asana_project_gid` | yes | From `ASANA_PROJECT_GID` |
| `github_project_owner` | for project / column sync | Projects v2 owner |
| `github_project_number` | for project / column sync | Projects v2 number |
| `github_repo` | for repo issues and G2A single-item | `owner/repo` |
| `dry_run` | no | Preview without writing |
| `sync_completed` | no | A2G also pulls completed Asana tasks |
| `default_labels` | no | Comma-separated labels for A2G repo issues |
| `issue_number` | G2A single-item | Issue or pull request number |
| `columns_only` | no | G2A only syncs Status → section |
| `sync_all_project_items` | no | G2A syncs every board item |
| `github_project_status` | no | Override Status; skips GraphQL lookup |
| `asana_github_issue_field_gid` | no | Override for the `GitHub Issue #` field |

## Versioning

- Pin `@v1` in consuming workflows.
- Pin `@v1.0.0` (or another exact tag) if you want a fixed revision.
- After each `v1.x.y` release, the `v1` tag moves to that commit.

How to cut a release and publish the Marketplace listing: [RELEASE.md](RELEASE.md).

## Marketplace listing

The composite action is what GitHub lists in **Actions** search, alongside actions such as `actions/checkout`. Requirements:

- This repository is public.
- A root `LICENSE` file is present (GitHub requires one for Marketplace).
- Root [`action.yml`](action.yml) includes `name`, `description`, and `branding`.
- You create a GitHub Release and publish the action to the Marketplace.

That listing is not the same as GitHub's curated starter workflows (Pages and similar). Marketplace is the path for this action.

## How sync works

The tools link items with:

1. A hidden marker in the GitHub issue or draft body: `<!-- asana-task-gid:{gid} -->`
2. An Asana custom field named `GitHub Issue #` that matches the GitHub issue number

| GitHub | Asana |
| --- | --- |
| title | task name |
| body | notes |
| open/closed (+ Status `Done`) | completed |
| labels | Labels / Issue Type / Priority / Story Points (`sp:3`) |
| Projects **Status** | project section (column) |

Status names match Asana sections flexibly — for example `Ready` → `Ready / Sprint`.

**Project mode** (A2G default): set `GITHUB_PROJECT_OWNER` and `GITHUB_PROJECT_NUMBER`. Creates draft issues on the board.

**Repo mode**: set `GITHUB_REPO=owner/repo` to create real issues. You can still set the project variables to link those issues onto a board. In this mode the sync writes the GitHub issue number into Asana's `GitHub Issue #` field and attaches the issue URL.

## Run locally

Local CLI setup, token scopes, and how this repository's own workflows run: [USAGE.md](USAGE.md).
