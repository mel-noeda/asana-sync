# Usage

This repository exposes two sync commands and a composite GitHub Action:

- **A2G** — pull Asana tasks into GitHub (Projects draft issues or repository issues)
- **G2A** — push a GitHub issue or pull request into its linked Asana task

To add the action to another repository, follow [README.md](README.md). This page covers tokens, local CLI use, and the workflows that run in *this* repository.

## Access tokens

Create the tokens once, then store them in a local `.env` file or in repository secrets. Do not commit tokens, and do not pass them as `workflow_dispatch` inputs — those values appear in the workflow run UI.

### Asana personal access token

Create a personal access token at [Asana's app settings](https://app.asana.com/0/my-apps). Set it as `ASANA_TOKEN`.

The Asana user who owns the token must be a member of the target project. A2G writes the GitHub issue number and attachment back onto tasks. G2A updates:

- task name
- notes
- completion
- custom fields
- project section

The project identifier is `ASANA_PROJECT_GID`: the number in the project URL (`…/0/<GID>/list`).

### GitHub personal access token

Create a classic or fine-grained personal access token (PAT). Locally, set it as `GITHUB_TOKEN`. In GitHub Actions, store it as `ASANA_SYNC_GITHUB_TOKEN` — the name `GITHUB_TOKEN` is reserved for the built-in job token, which does not have the Projects access these scripts need.

If the owning organization requires SAML single sign-on (SSO), authorize the PAT for that organization.

**A2G** writes to GitHub (creates project items and, in repo mode, issues). Grant:

- Classic PAT: `project`. Add `repo` when you set `GITHUB_REPO`.
- Fine-grained PAT: Projects **Read and write**. For repo mode, also grant the target repository Issues **Read and write**.

**G2A** only reads GitHub (issue or pull request, plus Projects **Status**). Grant:

- Classic PAT: `repo` and `project`.
- Fine-grained PAT: Issues **Read**, and Projects **Read**.

## Run locally

### Prerequisites

1. Install [uv](https://docs.astral.sh/uv/).
2. From the repository root, run `uv sync`.
3. Put credentials in `.env` (or export them in your shell).

Required for both commands:

- `ASANA_TOKEN`
- `ASANA_PROJECT_GID`
- `GITHUB_TOKEN`

Target at least one GitHub destination:

- Project mode: `GITHUB_PROJECT_OWNER` and `GITHUB_PROJECT_NUMBER`
- Repo mode: `GITHUB_REPO` as `owner/repo`

You can set both. A2G then creates real issues and also links them onto the board. G2A single-issue mode requires `GITHUB_REPO`. Column sync requires the project variables.

Optional:

- `DRY_RUN=true` — preview without writing
- `SYNC_COMPLETED=true` — A2G also pulls completed Asana tasks
- `DEFAULT_LABELS` — comma-separated labels for A2G repo issues
- `ASANA_GITHUB_ISSUE_FIELD_GID` — override for the `GitHub Issue #` custom field

### Asana → GitHub

```bash
uv run A2G
```

Project mode (default) creates draft issues on the Projects v2 board. Repo mode creates issues in `GITHUB_REPO` and writes the issue number into Asana's `GitHub Issue #` field.

### GitHub → Asana

Sync one issue or pull request:

```bash
uv run G2A 42
uv run G2A --issue 42
```

Reconcile **Status** on every board item into the matching Asana section:

```bash
uv run G2A --all-project-items --columns-only
```

G2A finds the Asana task from the `<!-- asana-task-gid:… -->` marker in the issue body, or from a task whose `GitHub Issue #` field matches the issue number.

## Workflows in this repository

These jobs call the local composite action (`uses: ./`) so this repo dogfoods the same packaging that consumers use.

Store tokens and config under **Settings → Secrets and variables → Actions**:

| Secret | Purpose |
| --- | --- |
| `ASANA_TOKEN` | Asana personal access token |
| `ASANA_SYNC_GITHUB_TOKEN` | GitHub PAT (mapped to `github_token` on the action) |
| `ASANA_PROJECT_GID` | Asana project GID |
| `GITHUB_PROJECT_OWNER` | Projects v2 owner (org or user) |
| `GITHUB_PROJECT_NUMBER` | Projects v2 number |
| `GITHUB_REPO` | `owner/repo` for real issues (G2A falls back to the current repository) |

You can start a run from the GitHub UI (**Actions → the workflow → Run workflow**) or with `gh` as below.

### Asana sync (`asana-sync.yml`)

Runs the action with `mode: a2g`. GitHub also starts this workflow on an hourly schedule, on push to `main` or `master`, and on selected issue and pull request events.

```bash
gh workflow run asana-sync.yml
```

Preview without creating GitHub items, or include completed Asana tasks:

```bash
gh workflow run asana-sync.yml \
  -f dry_run=true \
  -f sync_completed=true
```

Override the GitHub destination for one run (secrets still supply the tokens):

```bash
gh workflow run asana-sync.yml \
  -f github_project_owner=my-org \
  -f github_project_number=1 \
  -f github_repo=my-org/my-repo
```

### GitHub → Asana (`github-to-asana.yml`)

Runs the action with `mode: g2a`. GitHub also starts this workflow when an issue or pull request changes, and every two minutes to reconcile board columns. Actions cannot trigger natively on `projects_v2_item`; the schedule is the built-in stopgap for column moves.

Sync one issue or pull request:

```bash
gh workflow run github-to-asana.yml -f issue_number=42
```

Reconcile every board item's **Status** into Asana sections:

```bash
gh workflow run github-to-asana.yml \
  -f sync_all_project_items=true \
  -f columns_only=true
```

If you omit `issue_number` and leave `sync_all_project_items` unset, the workflow reconciles all project columns.

### Near-real-time column moves

Forward an organization or user `projects_v2_item` webhook to this repository as `repository_dispatch` with type `github_project_item`. The job runs G2A in columns-only mode for that item:

```bash
gh api repos/<owner>/<repo>/dispatches \
  -f event_type=github_project_item \
  -f client_payload[issue_number]=42 \
  -f client_payload[github_repo]=owner/repo \
  -f client_payload[status]='In progress'
```

`status` is optional. When present, G2A uses it as `GITHUB_PROJECT_STATUS` and skips the Projects Status lookup.
