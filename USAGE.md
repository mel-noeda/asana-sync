# Usage

This repository exposes two sync commands and a composite GitHub Action:

- **A2G** — pull Asana tasks into GitHub repository issues (a Projects board is optional)
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

The project identifier is `ASANA_PROJECT_GID` (or `--project-gid`): the number in the project URL (`…/0/<GID>/list`). Pass it as a workflow input when you run A2G for more than one Asana project. Always quote that GID in YAML (`asana_project_gid: "1210771720905224"`). An unquoted value becomes a float such as `1.21E+15`, which Asana rejects.

### GitHub personal access token

Create a classic or fine-grained personal access token (PAT). Locally, set it as `GITHUB_TOKEN` (or `GH_TOKEN`). In GitHub Actions, store the PAT as `GH_TOKEN` — the name `GITHUB_TOKEN` is reserved for the built-in job token.

You can omit `GH_TOKEN` in Actions when the target `github_repo` is this repository and you do not configure GitHub Projects. The action compares `github_repo` to `GITHUB_REPOSITORY` and uses the job token. Grant `issues: write` (A2G) or `issues: read` (G2A) on the workflow. A PAT is required for another repository or for Projects v2.

If the owning organization requires SAML single sign-on (SSO), authorize the PAT for that organization.

**A2G** writes to GitHub (creates issues, and optionally project items). Grant:

- Classic PAT: `repo`. Add `project` when you also link issues onto a Projects board.
- Fine-grained PAT: Issues **Read and write** on the target repository. Add Projects **Read and write** when you link a board.

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
- `ASANA_PROJECT_GID` (or A2G `--project-gid`)
- `GITHUB_TOKEN` (or `GH_TOKEN`)

A2G destination:

- Repository issues: `GITHUB_REPO` / `--repo` as `owner/repo`
- Optional Projects link: `GITHUB_PROJECT_OWNER` and `GITHUB_PROJECT_NUMBER`
- Project-only drafts: omit the repo and set the project variables

G2A single-issue mode requires `GITHUB_REPO`. Column sync requires the project variables.

Optional:

- `--section` / `ASANA_SECTION` — A2G only imports tasks in these Asana sections (comma-separated)
- `--status-field` / `ASANA_STATUS_FIELD` — match `--section` against this custom field instead of a board section
- `--dry-run` / `DRY_RUN=true` — preview without writing
- `--sync-completed` / `SYNC_COMPLETED=true` — A2G also pulls completed Asana tasks
- `DEFAULT_LABELS` — comma-separated labels for A2G repo issues
- `ASANA_GITHUB_ISSUE_FIELD_GID` — override for the `GitHub Issue #` custom field

### Asana → GitHub

```bash
uv run A2G --repo owner/repo --section "In Progress"
uv run A2G --project-gid 111 --repo owner/repo --section "In Progress"
uv run A2G --section "In Progress" --status-field Status
```

Each invocation reads one Asana project. Run the command again (or add another workflow job) for a second project. Repo mode creates issues in `--repo` / `GITHUB_REPO` and writes the issue number into Asana's `GitHub Issue #` field.

Omit `--section` to import every incomplete task, as before.

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
| `GH_TOKEN` | GitHub PAT (optional when the target is this repository and Projects is unset) |

Optional fallbacks when a run does not pass the matching input:

- `ASANA_PROJECT_GID`
- `GITHUB_REPO`
- `GITHUB_PROJECT_OWNER` and `GITHUB_PROJECT_NUMBER`

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

Override the Asana project, destination repo, and import section for one run (secrets still supply the tokens):

```bash
gh workflow run asana-sync.yml \
  -f asana_project_gid=111 \
  -f github_repo=my-org/my-repo \
  -f asana_section="In Progress"
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
