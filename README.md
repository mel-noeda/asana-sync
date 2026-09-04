# asana-sync

Two-way sync between Asana and GitHub:

- **A2G** — pull Asana tasks into GitHub repository issues (a Projects board is optional)
- **G2A** — push a GitHub issue or pull request into its linked Asana task, including the board column

Use this repository as a GitHub Action. Other repositories add a short workflow and secrets. They do not copy Python scripts.

## Use in another repository

1. Create the secrets below under **Settings → Secrets and variables → Actions**.
2. Add a workflow that calls `mel-noeda/asana-sync@v1`.
3. Pin `@v1` so you receive compatible patches, or pin a full tag such as `@v1.0.0`.

The action installs Python 3.14 on the runner through `uv`. You do not need Python in the consuming repository.

Copy-paste starters live in [`examples/`](examples/).

### Secrets

Store tokens as secrets only. Do not pass them as `workflow_dispatch` inputs — those values appear in the workflow run UI.

| Secret | Purpose |
| --- | --- |
| `ASANA_TOKEN` | Asana personal access token |
| `GH_TOKEN` | GitHub PAT — required for another repository or GitHub Projects |

You do not need `GH_TOKEN` when A2G writes issues only in this repository and you omit GitHub Projects. The action then uses the built-in job token. Grant `issues: write` on that workflow.

The built-in job token is named `GITHUB_TOKEN` and is reserved, so the PAT secret is `GH_TOKEN`.

Pass Asana project GIDs and destination repos as `with:` inputs. These optional secrets are fallbacks when a workflow omits the matching input:

- `ASANA_PROJECT_GID`
- `GITHUB_REPO`
- `GITHUB_PROJECT_OWNER` and `GITHUB_PROJECT_NUMBER` (optional Projects v2 link)

Token scopes and local setup are in [USAGE.md](USAGE.md).

### Asana → GitHub

Run one A2G invocation per Asana project. Each row creates issues in the configured GitHub repo when the task sits in the named Asana section (default example: `In Progress`). GitHub Projects is optional.

```yaml
# .github/workflows/asana-to-github.yml
name: Asana → GitHub
on:
  schedule:
    - cron: "0 * * * *"
  workflow_dispatch:
jobs:
  import:
    strategy:
      matrix:
        include:
          - asana_project_gid: "111"
          - asana_project_gid: "222"
          - asana_project_gid: "333"
            github_repo: org/other
    uses: mel-noeda/asana-sync/.github/workflows/reusable-asana-to-github.yml@v1
    secrets: inherit
    with:
      asana_project_gid: ${{ matrix.asana_project_gid }}
      github_repo: ${{ matrix.github_repo || github.repository }}
      asana_section: In Progress
```

Rows that target this repository do not need `GH_TOKEN`. A row whose `github_repo` is another repository does.

To call the composite action from a single job instead, see [`examples/asana-to-github.yml`](examples/asana-to-github.yml). To match a custom field instead of a board section, set `asana_status_field` (for example `Status`) and keep `asana_section` as the value to match.

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
| `github_token` | for another repo or Projects | From secret `GH_TOKEN`. Omit when the target is this repository and Projects is unset |
| `asana_project_gid` | yes | Asana project GID (input, not a secret) |
| `github_repo` | for repo issues and G2A single-item | Destination `owner/repo` |
| `asana_section` | no | A2G: only import tasks in this section (comma-separated) |
| `asana_status_field` | no | A2G: match `asana_section` against this custom field |
| `github_project_owner` | for optional project / column sync | Projects v2 owner |
| `github_project_number` | for optional project / column sync | Projects v2 number |
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

**Repo mode** (the usual A2G destination): set `github_repo` / `GITHUB_REPO` as `owner/repo`. The sync writes the GitHub issue number into Asana's `GitHub Issue #` field and attaches the issue URL. You can still set the project inputs to link those issues onto a board.

**Project-only mode**: omit the repo and set `github_project_owner` plus `github_project_number`. Creates draft issues on the board.

A2G does not close or delete a GitHub issue when the Asana task later leaves the gated section.

## Run locally

Local CLI setup, token scopes, and how this repository's own workflows run: [USAGE.md](USAGE.md).
