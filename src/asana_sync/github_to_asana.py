#!/usr/bin/env python3
"""
github_to_asana.py

Push a GitHub issue/PR update into the linked Asana task.

Designed for fast, targeted sync from GitHub Actions on issue/PR events:
fetch one issue by number, resolve its Asana task, and update that task only.
Issues with no linked Asana task are skipped (exit 0).

Also syncs GitHub Projects v2 Status (board column) → Asana project section.

Linking:
- Prefer the hidden marker in the issue body: <!-- asana-task-gid:{gid} -->
- Fallback: Asana project task whose "GitHub Issue #" or "GitHub Issue"
  custom field matches the issue number

Synced fields:
- title → Asana task name
- body (marker/meta stripped) → Asana notes
- state (closed/open) → Asana completed
- labels → Asana Labels / Issue Type / Priority / Story Points (by name match)
- Projects v2 Status → Asana section (column), e.g. "In progress" → "In Progress"

Setup:
    uv sync

Required environment variables:
    ASANA_TOKEN           Asana personal access token
    ASANA_PROJECT_GID     Asana project used to resolve custom fields / search
    GITHUB_TOKEN          GitHub PAT with repo + project read access

Issue selection (one of):
    CLI:    uv run G2A 42
            uv run G2A --issue 42
            uv run G2A --all-project-items [--columns-only]
    Env:    GITHUB_ISSUE_NUMBER=42

Also required for single-issue mode, and for --all-project-items
when no Projects board is configured:
    GITHUB_REPO           Repo as "owner/repo"

Optional column sync (Projects v2 Status → Asana section):
    GITHUB_PROJECT_OWNER  Projects v2 owner (org or user)
    GITHUB_PROJECT_NUMBER Projects v2 number

Optional:
    DRY_RUN               "true" to preview without writing to Asana
    GITHUB_PROJECT_STATUS Override Status name (skips GraphQL lookup)
    ASANA_GITHUB_ISSUE_FIELD_GID
                          Override for the "GitHub Issue #" / "GitHub Issue" field GID
"""

from __future__ import annotations

import argparse
import re
import sys
import time

import requests

from asana_sync.asana_columns import match_asana_section, normalize_column_key
from asana_sync.asana_to_github import GITHUB_ISSUE_FIELD_NAMES
from asana_sync.config import config

ASANA_BASE = "https://app.asana.com/api/1.0"
GITHUB_API = "https://api.github.com"
GITHUB_GRAPHQL = "https://api.github.com/graphql"

MARKER_RE = re.compile(r"<!--\s*asana-task-gid:(\d+)\s*-->")
META_LINE_RE = re.compile(
    r"^(?:\*\*Assignee:\*\*.*|\*\*Due:\*\*.*|\[View in Asana\]\([^)]*\))\s*$",
    re.MULTILINE,
)
STORY_POINTS_RE = re.compile(r"^(?:sp|story[_-]?points)[:\s-]*(\d+(?:\.\d+)?)$", re.I)
DONE_STATUS_KEYS = frozenset({"done", "complete", "completed"})

FIELD_LABELS = "Labels"
FIELD_ISSUE_TYPE = "Issue Type"
FIELD_PRIORITY = "Priority"
FIELD_STORY_POINTS = "Story Points"
STATUS_FIELD_NAME = "Status"


def _asana_headers(token):
    return {"Authorization": f"Bearer {token}"}


def _gh_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _http_error_detail(exc: requests.HTTPError) -> str:
    resp = exc.response
    detail = str(exc)
    if resp is not None:
        try:
            message = resp.json().get("message")
        except ValueError:
            message = None
        if message:
            detail = f"{exc} ({message})"
    return detail


# ----------------------------- GitHub -------------------------------------- #


def github_get_issue(token, repo, number):
    resp = requests.get(
        f"{GITHUB_API}/repos/{repo}/issues/{number}",
        headers=_gh_headers(token),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def graphql(token, query, variables):
    resp = requests.post(
        GITHUB_GRAPHQL,
        headers={"Authorization": f"Bearer {token}"},
        json={"query": query, "variables": variables},
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("errors"):
        raise RuntimeError(payload["errors"])
    return payload["data"]


def get_project_node_id(token, owner, number):
    """Resolve a Projects v2 board's node ID. Tries org, then user."""
    number = int(number)
    for kind in ("organization", "user"):
        query = f"query($owner:String!, $number:Int!){{  {kind}(login:$owner){{ projectV2(number:$number){{ id }} }}}}"
        try:
            data = graphql(token, query, {"owner": owner, "number": number})
        except RuntimeError:
            continue
        holder = data.get(kind)
        if holder and holder.get("projectV2"):
            return holder["projectV2"]["id"]
    return None


def github_issue_project_status(token, repo, number, project_number, *, is_pr=False):
    """Return Status name for an issue/PR on the given Projects v2 board, if any."""
    owner, name = repo.split("/", 1)
    project_number = int(project_number)
    root = "pullRequest" if is_pr else "issue"
    query = f"""
    query($owner:String!, $name:String!, $number:Int!) {{
      repository(owner:$owner, name:$name) {{
        {root}(number:$number) {{
          projectItems(first: 30) {{
            nodes {{
              project {{ number }}
              fieldValueByName(name:"{STATUS_FIELD_NAME}") {{
                ... on ProjectV2ItemFieldSingleSelectValue {{ name }}
              }}
            }}
          }}
        }}
      }}
    }}
    """
    data = graphql(
        token,
        query,
        {"owner": owner, "name": name, "number": int(number)},
    )
    items = (((data.get("repository") or {}).get(root) or {}).get("projectItems") or {}).get("nodes") or []
    for item in items:
        project = item.get("project") or {}
        if project.get("number") != project_number:
            continue
        field = item.get("fieldValueByName") or {}
        status = field.get("name")
        if status:
            return status
    return None


def iter_project_items(token, project_id):
    """Yield {repo, number, status, body, state, title, is_pr} for Issue/PR items."""
    cursor = None
    query = """
    query($id: ID!, $cursor: String) {
      node(id: $id) {
        ... on ProjectV2 {
          items(first: 50, after: $cursor) {
            pageInfo { hasNextPage endCursor }
            nodes {
              fieldValueByName(name: "Status") {
                ... on ProjectV2ItemFieldSingleSelectValue { name }
              }
              content {
                __typename
                ... on Issue {
                  number
                  title
                  body
                  state
                  repository { nameWithOwner }
                }
                ... on PullRequest {
                  number
                  title
                  body
                  state
                  repository { nameWithOwner }
                }
              }
            }
          }
        }
      }
    }
    """
    while True:
        data = graphql(token, query, {"id": project_id, "cursor": cursor})
        connection = (data.get("node") or {}).get("items") or {}
        for node in connection.get("nodes") or []:
            content = node.get("content") or {}
            typename = content.get("__typename")
            if typename not in ("Issue", "PullRequest"):
                continue
            repo = (content.get("repository") or {}).get("nameWithOwner")
            number = content.get("number")
            if not repo or not number:
                continue
            status = ((node.get("fieldValueByName") or {}).get("name")) or None
            yield {
                "repo": repo,
                "number": number,
                "status": status,
                "body": content.get("body"),
                "state": (content.get("state") or "").lower(),
                "title": content.get("title") or "",
                "is_pr": typename == "PullRequest",
            }
        page = connection.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            break
        cursor = page.get("endCursor")


def iter_repo_issues(token, repo):
    """Yield every issue and pull request in the repo (REST, paginated)."""
    page = 1
    while True:
        resp = requests.get(
            f"{GITHUB_API}/repos/{repo}/issues",
            headers=_gh_headers(token),
            params={"state": "all", "per_page": 100, "page": page},
            timeout=30,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        yield from batch
        page += 1


def issue_label_names(issue):
    names = []
    for label in issue.get("labels") or []:
        if isinstance(label, dict):
            name = label.get("name")
        else:
            name = str(label)
        if name:
            names.append(name)
    return names


def body_for_asana(body: str | None) -> str:
    """Strip sync markers / footer meta so we don't echo them into Asana notes."""
    text = body or ""
    text = MARKER_RE.sub("", text)
    text = META_LINE_RE.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def asana_gid_from_issue_body(body: str | None) -> str | None:
    match = MARKER_RE.search(body or "")
    return match.group(1) if match else None


# ----------------------------- Asana --------------------------------------- #


def asana_get_project(token, project_gid):
    resp = requests.get(
        f"{ASANA_BASE}/projects/{project_gid}",
        headers=_asana_headers(token),
        params={"opt_fields": "workspace.gid"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["data"]


def asana_custom_field_settings(token, project_gid):
    resp = requests.get(
        f"{ASANA_BASE}/projects/{project_gid}/custom_field_settings",
        headers=_asana_headers(token),
        params={
            "opt_fields": (
                "custom_field.name,custom_field.gid,custom_field.resource_subtype,"
                "custom_field.enum_options.name,custom_field.enum_options.gid,"
                "custom_field.enum_options.enabled"
            )
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("data", [])


def asana_list_sections(token, project_gid):
    resp = requests.get(
        f"{ASANA_BASE}/projects/{project_gid}/sections",
        headers=_asana_headers(token),
        params={"opt_fields": "name,gid"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("data", [])


def asana_move_task_to_section(token, section_gid, task_gid):
    resp = requests.post(
        f"{ASANA_BASE}/sections/{section_gid}/addTask",
        headers={**_asana_headers(token), "Content-Type": "application/json"},
        json={"data": {"task": task_gid}},
        timeout=30,
    )
    resp.raise_for_status()


def index_custom_fields(settings):
    """Map field name -> {gid, subtype, options_by_name}."""
    by_name = {}
    for setting in settings:
        field = setting.get("custom_field") or {}
        name = field.get("name")
        if not name:
            continue
        options = {}
        for opt in field.get("enum_options") or []:
            if not opt.get("enabled", True):
                continue
            opt_name = opt.get("name") or ""
            options[opt_name.casefold()] = opt["gid"]
            prefix = opt_name.split("-", 1)[0].strip().casefold()
            if prefix and prefix not in options:
                options[prefix] = opt["gid"]
        by_name[name] = {
            "gid": field["gid"],
            "subtype": field.get("resource_subtype") or field.get("type"),
            "options": options,
        }
    return by_name


def github_issue_field(fields_by_name):
    """Return the GitHub Issue # or GitHub Issue field entry, if present."""
    for name in GITHUB_ISSUE_FIELD_NAMES:
        field = fields_by_name.get(name)
        if field:
            return field
    return None


def asana_find_task_by_issue_number(token, workspace_gid, project_gid, field_gid, issue_number):
    """Search the project for a task whose GitHub Issue # field matches."""
    resp = requests.get(
        f"{ASANA_BASE}/workspaces/{workspace_gid}/tasks/search",
        headers=_asana_headers(token),
        params={
            "projects.any": project_gid,
            f"custom_fields.{field_gid}.value": str(issue_number),
            "opt_fields": "gid,name",
            "limit": 10,
        },
        timeout=30,
    )
    resp.raise_for_status()
    tasks = resp.json().get("data", [])
    return tasks[0] if tasks else None


def asana_update_task(token, task_gid, data):
    resp = requests.put(
        f"{ASANA_BASE}/tasks/{task_gid}",
        headers={**_asana_headers(token), "Content-Type": "application/json"},
        json={"data": data},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["data"]


def map_labels_to_custom_fields(label_names, fields_by_name):
    """Map GitHub label names onto Asana custom fields."""
    custom_fields = {}
    unmatched = []

    labels_field = fields_by_name.get(FIELD_LABELS)
    type_field = fields_by_name.get(FIELD_ISSUE_TYPE)
    priority_field = fields_by_name.get(FIELD_PRIORITY)
    points_field = fields_by_name.get(FIELD_STORY_POINTS)

    label_option_gids = []
    type_gid = None
    priority_gid = None
    story_points = None

    for raw in label_names:
        key = raw.casefold().strip()
        sp_match = STORY_POINTS_RE.match(raw.strip())
        if sp_match and points_field:
            story_points = float(sp_match.group(1))
            continue

        if labels_field and key in labels_field["options"]:
            label_option_gids.append(labels_field["options"][key])
            continue
        if type_field and key in type_field["options"]:
            type_gid = type_field["options"][key]
            continue
        if priority_field and key in priority_field["options"]:
            priority_gid = priority_field["options"][key]
            continue
        unmatched.append(raw)

    if labels_field:
        custom_fields[labels_field["gid"]] = label_option_gids
    if type_field and type_gid is not None:
        custom_fields[type_field["gid"]] = type_gid
    if priority_field and priority_gid is not None:
        custom_fields[priority_field["gid"]] = priority_gid
    if points_field and story_points is not None:
        custom_fields[points_field["gid"]] = story_points

    return custom_fields, unmatched


def build_task_update(issue, fields_by_name, github_issue_field_gid, *, status_name=None):
    label_names = issue_label_names(issue)
    custom_fields, unmatched = map_labels_to_custom_fields(label_names, fields_by_name)
    if github_issue_field_gid:
        custom_fields[github_issue_field_gid] = str(issue["number"])

    completed = issue.get("state") == "closed"
    if status_name and normalize_column_key(status_name) in DONE_STATUS_KEYS:
        completed = True

    data = {
        "name": issue.get("title") or "(untitled GitHub issue)",
        "notes": body_for_asana(issue.get("body")),
        "completed": completed,
    }
    if custom_fields:
        data["custom_fields"] = custom_fields
    return data, unmatched


# ------------------------------- Glue -------------------------------------- #


def resolve_asana_task(asana_token, project_gid, issue, fields_by_name, *, workspace_gid=None):
    """Return (task_gid, source) for the Asana task linked to this issue."""
    from_marker = asana_gid_from_issue_body(issue.get("body"))
    if from_marker:
        return from_marker, "issue body marker"

    github_field = github_issue_field(fields_by_name)
    field_gid = config.asana_github_issue_field_gid or (github_field or {}).get("gid")
    if not field_gid:
        return None, None

    if not workspace_gid:
        project = asana_get_project(asana_token, project_gid)
        workspace_gid = (project.get("workspace") or {}).get("gid")
    if not workspace_gid:
        return None, None

    found = asana_find_task_by_issue_number(
        asana_token,
        workspace_gid,
        project_gid,
        field_gid,
        issue["number"],
    )
    if found:
        return found["gid"], "GitHub Issue # custom field"
    return None, None


def resolve_status_name(gh_token, repo, issue, project_owner, project_number):
    """Prefer explicit env override, else look up Projects v2 Status."""
    override = config.github_project_status
    if override:
        return override

    if not (project_owner and project_number):
        return None

    is_pr = "pull_request" in issue
    try:
        return github_issue_project_status(
            gh_token,
            repo,
            issue["number"],
            project_number,
            is_pr=is_pr,
        )
    except (requests.HTTPError, RuntimeError) as exc:
        detail = _http_error_detail(exc) if isinstance(exc, requests.HTTPError) else str(exc)
        print(f"Warning: could not read Projects Status for #{issue['number']}: {detail}")
        return None


def sync_column(asana_token, task_gid, status_name, sections, *, dry_run=False):
    """Move the Asana task into the section matching GitHub Status. Returns message."""
    if not status_name:
        return "no Status on project item"

    section = match_asana_section(status_name, sections)
    if not section:
        return f"no Asana section matched Status {status_name!r}"

    if dry_run:
        return f"would move to section {section['name']!r} (from Status {status_name!r})"

    asana_move_task_to_section(asana_token, section["gid"], task_gid)
    return f"moved to section {section['name']!r} (from Status {status_name!r})"


def sync_one_issue(
    *,
    asana_token,
    gh_token,
    asana_project_gid,
    repo,
    issue,
    fields_by_name,
    github_issue_field_gid,
    sections,
    project_owner,
    project_number,
    workspace_gid,
    columns_only=False,
    dry_run=False,
    status_name=None,
):
    task_gid, source = resolve_asana_task(
        asana_token,
        asana_project_gid,
        issue,
        fields_by_name,
        workspace_gid=workspace_gid,
    )
    if not task_gid:
        return False, "no linked Asana task"

    if status_name is None:
        status_name = resolve_status_name(gh_token, repo, issue, project_owner, project_number)

    actions = [f"task {task_gid} via {source}"]

    if columns_only:
        completed = issue.get("state") == "closed"
        if status_name and normalize_column_key(status_name) in DONE_STATUS_KEYS:
            completed = True
        if dry_run:
            actions.append(f"would set completed={completed}")
        else:
            asana_update_task(asana_token, task_gid, {"completed": completed})
            actions.append(f"set completed={completed}")
    else:
        update, unmatched = build_task_update(
            issue,
            fields_by_name,
            github_issue_field_gid,
            status_name=status_name,
        )
        if unmatched:
            actions.append(f"unmatched labels: {', '.join(unmatched)}")
        summary = (
            f"name={update['name']!r}, completed={update['completed']}, notes_chars={len(update.get('notes') or '')}"
        )
        if dry_run:
            actions.append(f"would update {summary}")
        else:
            asana_update_task(asana_token, task_gid, update)
            actions.append(f"updated {summary}")

    if sections is not None:
        try:
            col_msg = sync_column(
                asana_token,
                task_gid,
                status_name,
                sections,
                dry_run=dry_run,
            )
            actions.append(col_msg)
        except requests.HTTPError as exc:
            actions.append(f"column sync failed: {_http_error_detail(exc)}")

    return True, "; ".join(actions)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Sync a GitHub issue/PR into its linked Asana task.")
    parser.add_argument("issue", nargs="?", type=int, help="GitHub issue or PR number")
    parser.add_argument(
        "--issue",
        "-i",
        dest="issue_flag",
        type=int,
        help="GitHub issue or PR number (alternative to positional)",
    )
    parser.add_argument(
        "--all-project-items",
        action="store_true",
        help="Sync every Issue/PR on the Projects v2 board, or every repo issue when no board is set",
    )
    parser.add_argument(
        "--columns-only",
        action="store_true",
        help="Only sync Status→Asana section (and completion), not title/body/labels",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    columns_only = args.columns_only or config.columns_only
    all_items = args.all_project_items or config.sync_all_project_items

    asana_token = config.asana_token
    asana_project_gid = config.asana_project_gid
    gh_token = config.require_github_token(config.github_repo)
    dry_run = config.dry_run

    project_owner = config.github_project_owner
    project_number = config.github_project_number

    settings = asana_custom_field_settings(asana_token, asana_project_gid)
    fields_by_name = index_custom_fields(settings)
    github_field = github_issue_field(fields_by_name)
    github_issue_field_gid = config.asana_github_issue_field_gid or (github_field or {}).get("gid")

    asana_project = asana_get_project(asana_token, asana_project_gid)
    workspace_gid = (asana_project.get("workspace") or {}).get("gid")

    sections = None
    if project_owner and project_number:
        sections = asana_list_sections(asana_token, asana_project_gid)
        print(f"Column sync enabled for {project_owner}/#{project_number} → {len(sections)} Asana section(s).")
    else:
        print("Note: set GITHUB_PROJECT_OWNER + GITHUB_PROJECT_NUMBER to sync Projects Status → Asana sections.")

    if all_items:
        if project_owner and project_number:
            project_id = get_project_node_id(gh_token, project_owner, project_number)
            if not project_id:
                sys.exit(f"Could not resolve Projects v2 board {project_owner}/#{project_number}")
            item_iter = iter_project_items(gh_token, project_id)
            source_is_project = True
        else:
            repo = config.github_repo
            if not repo:
                sys.exit("--all-project-items without a Projects board requires GITHUB_REPO")
            print(f"Reconciling all issues in {repo}.")
            item_iter = (
                {
                    "repo": repo,
                    "number": issue["number"],
                    "status": None,
                    "issue": issue,
                }
                for issue in iter_repo_issues(gh_token, repo)
            )
            source_is_project = False

        synced = skipped = failed = 0
        for item in item_iter:
            repo = item["repo"]
            number = item["number"]
            issue = item.get("issue")
            if issue is None:
                issue = {
                    "number": number,
                    "title": item["title"],
                    "body": item["body"],
                    "state": "closed" if item["state"] == "closed" else "open",
                    "labels": [],
                }
                if item.get("is_pr"):
                    issue["pull_request"] = {}

            # Project items omit labels; full sync needs REST. Repo listing already has them.
            if source_is_project and not columns_only:
                try:
                    issue = github_get_issue(gh_token, repo, number)
                except requests.HTTPError as exc:
                    print(f"  ! {repo}#{number}: {_http_error_detail(exc)}")
                    failed += 1
                    continue

            try:
                ok, message = sync_one_issue(
                    asana_token=asana_token,
                    gh_token=gh_token,
                    asana_project_gid=asana_project_gid,
                    repo=repo,
                    issue=issue,
                    fields_by_name=fields_by_name,
                    github_issue_field_gid=github_issue_field_gid,
                    sections=sections,
                    project_owner=project_owner,
                    project_number=project_number,
                    workspace_gid=workspace_gid,
                    columns_only=columns_only,
                    dry_run=dry_run,
                    status_name=item.get("status"),
                )
            except (requests.HTTPError, RuntimeError) as exc:
                detail = _http_error_detail(exc) if isinstance(exc, requests.HTTPError) else str(exc)
                print(f"  ! {repo}#{number}: {detail}")
                failed += 1
                continue

            if ok:
                print(f"{repo}#{number}: {message}")
                synced += 1
            else:
                print(f"{repo}#{number}: skipped ({message})")
                skipped += 1
            time.sleep(0.2)

        print(f"\nDone. synced={synced}, skipped={skipped}, failed={failed}.")
        return 0

    issue_number = args.issue_flag or args.issue or config.github_issue_number
    if not issue_number:
        sys.exit("Provide an issue number: uv run G2A <number> (or set GITHUB_ISSUE_NUMBER / use --all-project-items)")

    repo = config.github_repo
    if not repo:
        sys.exit("Missing required environment variable: GITHUB_REPO")

    print(f"Fetching {repo}#{issue_number}...")
    try:
        issue = github_get_issue(gh_token, repo, issue_number)
    except requests.HTTPError as exc:
        sys.exit(f"Failed to fetch GitHub issue: {_http_error_detail(exc)}")

    kind = "PR" if "pull_request" in issue else "issue"
    print(f"Loaded {kind} #{issue['number']}: {issue.get('title')!r} [{issue.get('state')}]")

    try:
        ok, message = sync_one_issue(
            asana_token=asana_token,
            gh_token=gh_token,
            asana_project_gid=asana_project_gid,
            repo=repo,
            issue=issue,
            fields_by_name=fields_by_name,
            github_issue_field_gid=github_issue_field_gid,
            sections=sections,
            project_owner=project_owner,
            project_number=project_number,
            workspace_gid=workspace_gid,
            columns_only=columns_only,
            dry_run=dry_run,
        )
    except (requests.HTTPError, RuntimeError) as exc:
        detail = _http_error_detail(exc) if isinstance(exc, requests.HTTPError) else str(exc)
        sys.exit(f"Sync failed: {detail}")

    if not ok:
        print(f"{repo}#{issue_number}: skipped ({message})")
        return 0
    print(message)
    return 0


if __name__ == "__main__":
    main()
