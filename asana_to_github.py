#!/usr/bin/env python3
"""
asana_to_github.py

Pull tasks from an Asana project and create corresponding GitHub Project items
(or optional repository issues).

Key properties:
- Idempotent: each item body carries a hidden marker with the Asana task GID.
  On re-run, tasks that already have an item are skipped, so you can run this
  repeatedly (cron, CI, manually) without creating duplicates.
- Default: create draft issues directly on a GitHub Projects (v2) board
  (no repository required).
- Optional: create real issues in a GitHub repository instead (and optionally
  also link them onto a Projects v2 board).
- When using GITHUB_REPO: write the GitHub issue number back to Asana's
  "GitHub Issue #" custom field and attach the issue URL on the task.
  Already-synced tasks are reconciled on every run.

Setup:
    uv sync

Required environment variables:
    ASANA_TOKEN             Asana personal access token
    ASANA_PROJECT_GID       The project GID to pull tasks from
                            (the number in the project URL: .../0/<GID>/list)
    GITHUB_TOKEN            GitHub PAT with `project` scope (add `repo` if using
                            GITHUB_REPO below)

Target (one of):
    GITHUB_PROJECT_OWNER    Org or user login that owns the Projects v2 board
    GITHUB_PROJECT_NUMBER   The project number (the integer in the project URL)
  or:
    GITHUB_REPO             Target repo as "owner/repo" (creates real issues)

Optional environment variables:
    ASANA_GITHUB_ISSUE_FIELD_GID
                            Custom field GID for "GitHub Issue #". If unset, the
                            field is resolved by name on the Asana project.
    SYNC_COMPLETED          "true" to also sync completed tasks (default: skip them)
    DEFAULT_LABELS          Comma-separated labels for repo issues only
    DRY_RUN                 "true" to preview without creating anything

Usage:
    uv run A2G
"""

import os
import re
import sys
import time

import requests
from dotenv import load_dotenv

load_dotenv()

ASANA_BASE = "https://app.asana.com/api/1.0"
GITHUB_API = "https://api.github.com"
GITHUB_GRAPHQL = "https://api.github.com/graphql"

MARKER_TEMPLATE = "<!-- asana-task-gid:{gid} -->"
MARKER_RE = re.compile(r"<!-- asana-task-gid:(\d+) -->")
DEFAULT_GITHUB_ISSUE_FIELD_NAME = "GitHub Issue #"


def env(name, default=None, required=False):
    val = os.environ.get(name, default)
    if required and not val:
        sys.exit(f"Missing required environment variable: {name}")
    return val


def _asana_headers(token):
    return {"Authorization": f"Bearer {token}"}


# ----------------------------- Asana --------------------------------------- #


def asana_get_tasks(token, project_gid):
    """Yield every task in a project, following pagination."""
    headers = _asana_headers(token)
    params = {
        "opt_fields": (
            "name,notes,completed,permalink_url,assignee.name,due_on,"
            "custom_fields,custom_fields.name,custom_fields.gid,"
            "custom_fields.text_value,custom_fields.display_value"
        ),
        "limit": 100,
    }
    url = f"{ASANA_BASE}/projects/{project_gid}/tasks"
    while url:
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        payload = resp.json()
        yield from payload.get("data", [])
        # next_page.uri is a fully-formed URL that already carries the params
        next_page = payload.get("next_page")
        if next_page and next_page.get("uri"):
            url = next_page["uri"]
            params = None
        else:
            url = None


def asana_find_custom_field_gid(token, project_gid, field_name=DEFAULT_GITHUB_ISSUE_FIELD_NAME):
    """Resolve a project custom field GID by display name."""
    resp = requests.get(
        f"{ASANA_BASE}/projects/{project_gid}/custom_field_settings",
        headers=_asana_headers(token),
        params={"opt_fields": "custom_field.name,custom_field.gid"},
        timeout=30,
    )
    resp.raise_for_status()
    for setting in resp.json().get("data", []):
        field = setting.get("custom_field") or {}
        if field.get("name") == field_name:
            return field.get("gid")
    return None


def task_custom_field_text(task, field_gid):
    for field in task.get("custom_fields") or []:
        if field.get("gid") == field_gid:
            return field.get("text_value") or field.get("display_value")
    return None


def asana_set_github_issue_number(token, task_gid, field_gid, issue_number):
    resp = requests.put(
        f"{ASANA_BASE}/tasks/{task_gid}",
        headers={**_asana_headers(token), "Content-Type": "application/json"},
        json={"data": {"custom_fields": {field_gid: str(issue_number)}}},
        timeout=30,
    )
    resp.raise_for_status()


def asana_list_attachments(token, task_gid):
    resp = requests.get(
        f"{ASANA_BASE}/attachments",
        headers=_asana_headers(token),
        params={
            "parent": task_gid,
            "opt_fields": "name,view_url,resource_subtype",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("data", [])


def asana_ensure_github_link(token, task_gid, issue_url, issue_number, title):
    """Attach the GitHub issue URL on the Asana task if it is not already linked."""
    for attachment in asana_list_attachments(token, task_gid):
        if attachment.get("view_url") == issue_url:
            return False
    resp = requests.post(
        f"{ASANA_BASE}/attachments",
        headers=_asana_headers(token),
        files={
            "parent": (None, task_gid),
            "resource_subtype": (None, "external"),
            "name": (None, f"#{issue_number} {title}"),
            "url": (None, issue_url),
        },
        timeout=30,
    )
    resp.raise_for_status()
    return True


def reconcile_asana_github_link(
    asana_token,
    task,
    issue,
    field_gid,
    *,
    dry_run=False,
):
    """
    Ensure the Asana task has the correct GitHub issue number and URL attachment.

    Returns a short status string describing what happened.
    """
    task_gid = task["gid"]
    issue_number = issue["number"]
    issue_url = issue["html_url"]
    title = issue.get("title") or task.get("name") or f"Issue #{issue_number}"
    actions = []

    current = task_custom_field_text(task, field_gid)
    desired = str(issue_number)
    if current != desired:
        if dry_run:
            actions.append(f"set GitHub Issue # to {desired}")
        else:
            asana_set_github_issue_number(asana_token, task_gid, field_gid, issue_number)
            actions.append(f"set GitHub Issue # to {desired}")

    if dry_run:
        actions.append(f"ensure link {issue_url}")
    else:
        added = asana_ensure_github_link(asana_token, task_gid, issue_url, issue_number, title)
        if added:
            actions.append(f"linked {issue_url}")

    if not actions:
        return "already linked"
    return ", ".join(actions)


# ----------------------------- GitHub -------------------------------------- #


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


def github_existing_synced_issues(token, repo):
    """
    Return mapping of Asana task GID -> GitHub issue info for issues that
    already carry an asana-task-gid marker.
    """
    by_gid = {}
    page = 1
    while True:
        resp = requests.get(
            f"{GITHUB_API}/repos/{repo}/issues",
            headers=_gh_headers(token),
            params={"state": "all", "per_page": 100, "page": page},
            timeout=30,
        )
        resp.raise_for_status()
        issues = resp.json()
        if not issues:
            break
        for issue in issues:
            if "pull_request" in issue:  # the issues endpoint also returns PRs
                continue
            body = issue.get("body") or ""
            for gid in MARKER_RE.findall(body):
                by_gid[gid] = {
                    "number": issue["number"],
                    "html_url": issue["html_url"],
                    "node_id": issue["node_id"],
                    "title": issue.get("title") or "",
                }
        page += 1
    return by_gid


def github_create_issue(token, repo, title, body, labels):
    data = {"title": title, "body": body}
    if labels:
        data["labels"] = labels
    resp = requests.post(
        f"{GITHUB_API}/repos/{repo}/issues",
        headers=_gh_headers(token),
        json=data,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()  # includes number, html_url, node_id


# ------------------------- GitHub Projects (v2) ---------------------------- #


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


def project_existing_synced_gids(token, project_id):
    """Return Asana GIDs already present on project draft issues / issues."""
    gids = set()
    cursor = None
    query = """
    query($id: ID!, $cursor: String) {
      node(id: $id) {
        ... on ProjectV2 {
          items(first: 100, after: $cursor) {
            pageInfo { hasNextPage endCursor }
            nodes {
              content {
                ... on DraftIssue { body }
                ... on Issue { body }
              }
            }
          }
        }
      }
    }
    """
    while True:
        data = graphql(token, query, {"id": project_id, "cursor": cursor})
        items = (data.get("node") or {}).get("items") or {}
        for node in items.get("nodes") or []:
            content = node.get("content") or {}
            body = content.get("body") or ""
            gids.update(MARKER_RE.findall(body))
        page = items.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            break
        cursor = page.get("endCursor")
    return gids


def project_create_draft_issue(token, project_id, title, body):
    mutation = """
    mutation($projectId: ID!, $title: String!, $body: String) {
      addProjectV2DraftIssue(input: {
        projectId: $projectId
        title: $title
        body: $body
      }) {
        projectItem { id }
      }
    }
    """
    data = graphql(
        token,
        mutation,
        {"projectId": project_id, "title": title, "body": body},
    )
    return data["addProjectV2DraftIssue"]["projectItem"]


def add_issue_to_project(token, project_id, issue_node_id):
    mutation = (
        "mutation($projectId:ID!, $contentId:ID!){"
        "  addProjectV2ItemById(input:{projectId:$projectId, contentId:$contentId}){ item { id } }"
        "}"
    )
    graphql(token, mutation, {"projectId": project_id, "contentId": issue_node_id})


# ------------------------------- Glue -------------------------------------- #


def build_body(task):
    parts = []
    notes = (task.get("notes") or "").strip()
    if notes:
        parts.append(notes)

    meta = []
    assignee = task.get("assignee") or {}
    if assignee.get("name"):
        meta.append(f"**Assignee:** {assignee['name']}")
    if task.get("due_on"):
        meta.append(f"**Due:** {task['due_on']}")
    if task.get("permalink_url"):
        meta.append(f"[View in Asana]({task['permalink_url']})")
    if meta:
        parts.append("\n".join(meta))

    parts.append(MARKER_TEMPLATE.format(gid=task["gid"]))
    return "\n\n".join(parts)


def main():
    asana_token = env("ASANA_TOKEN", required=True)
    project_gid = env("ASANA_PROJECT_GID", required=True)
    gh_token = env("GITHUB_TOKEN", required=True)

    repo = env("GITHUB_REPO")
    if repo and "/" not in repo:
        sys.exit(f'GITHUB_REPO must be "owner/repo", got: {repo!r}')

    project_owner = env("GITHUB_PROJECT_OWNER")
    project_number = env("GITHUB_PROJECT_NUMBER")
    if not repo and not (project_owner and project_number):
        sys.exit(
            "Configure a target: set GITHUB_PROJECT_OWNER + GITHUB_PROJECT_NUMBER "
            "(project draft issues), and/or GITHUB_REPO (repository issues)."
        )

    sync_completed = env("SYNC_COMPLETED", "false").lower() == "true"
    dry_run = env("DRY_RUN", "false").lower() == "true"
    labels = [label.strip() for label in (env("DEFAULT_LABELS", "") or "").split(",") if label.strip()]

    # Project-only mode (no repo): create draft issues on the board.
    # Repo mode: create real issues; project board is an optional extra link.
    project_only = not repo
    project_node_id = None
    if project_owner and project_number:
        if dry_run:
            project_node_id = "dry-run"
            print(f"Targeting Projects v2 board {project_owner}/#{project_number}.")
        else:
            project_node_id = get_project_node_id(gh_token, project_owner, project_number)
            if not project_node_id:
                sys.exit(f"Could not resolve Projects v2 board {project_owner}/#{project_number}.")
            if project_only:
                print(f"Creating draft issues on Projects v2 board {project_owner}/#{project_number}.")
            else:
                print(f"Linking new issues to Projects v2 board {project_owner}/#{project_number}.")

    github_issue_field_gid = None
    if not project_only:
        github_issue_field_gid = env("ASANA_GITHUB_ISSUE_FIELD_GID")
        if not github_issue_field_gid:
            github_issue_field_gid = asana_find_custom_field_gid(
                asana_token, project_gid, DEFAULT_GITHUB_ISSUE_FIELD_NAME
            )
        if github_issue_field_gid:
            print(
                f"Writing issue numbers back to Asana field "
                f"{DEFAULT_GITHUB_ISSUE_FIELD_NAME!r} ({github_issue_field_gid})."
            )
        else:
            print(
                f"Warning: Asana custom field {DEFAULT_GITHUB_ISSUE_FIELD_NAME!r} "
                "not found; issue numbers will not be written back."
            )

    print("Loading existing items to avoid duplicates...")
    existing_issues = {}
    existing_draft_gids = set()
    if dry_run and project_only:
        pass
    elif project_only:
        existing_draft_gids = project_existing_synced_gids(gh_token, project_node_id)
    else:
        existing_issues = github_existing_synced_issues(gh_token, repo)
    print(f"Found {len(existing_issues) or len(existing_draft_gids)} already-synced task(s).")

    created = skipped = linked = 0
    for task in asana_get_tasks(asana_token, project_gid):
        gid = task["gid"]
        name = task.get("name") or "(untitled Asana task)"

        if task.get("completed") and not sync_completed:
            skipped += 1
            continue

        body = build_body(task)

        # Repo mode: create or reconcile an existing GitHub issue + Asana link.
        if not project_only:
            issue = existing_issues.get(gid)
            if issue:
                if github_issue_field_gid:
                    try:
                        status = reconcile_asana_github_link(
                            asana_token,
                            task,
                            issue,
                            github_issue_field_gid,
                            dry_run=dry_run,
                        )
                        print(f"Existing #{issue['number']}: {name} -> {status}")
                        if status != "already linked":
                            linked += 1
                    except (requests.HTTPError, RuntimeError) as exc:
                        detail = _http_error_detail(exc) if isinstance(exc, requests.HTTPError) else str(exc)
                        print(f"  ! failed to link Asana task for '{name}': {detail}")
                else:
                    print(f"Existing #{issue['number']}: {name} (no Asana field to update)")
                skipped += 1
                continue

            if dry_run:
                print(f"[dry-run] would create issue: {name}")
                if github_issue_field_gid:
                    print("  [dry-run] would set GitHub Issue # and attach issue URL")
                created += 1
                continue

            try:
                issue = github_create_issue(gh_token, repo, name, body, labels)
            except requests.HTTPError as exc:
                print(f"  ! failed to create '{name}': {_http_error_detail(exc)}")
                continue

            print(f"Created #{issue['number']}: {name} -> {issue['html_url']}")
            if project_node_id:
                try:
                    add_issue_to_project(gh_token, project_node_id, issue["node_id"])
                except Exception as exc:  # noqa: BLE001 - report and continue
                    print(f"  ! could not add to project board: {exc}")

            if github_issue_field_gid:
                try:
                    status = reconcile_asana_github_link(
                        asana_token,
                        task,
                        issue,
                        github_issue_field_gid,
                        dry_run=False,
                    )
                    print(f"  Asana: {status}")
                    linked += 1
                except (requests.HTTPError, RuntimeError) as exc:
                    detail = _http_error_detail(exc) if isinstance(exc, requests.HTTPError) else str(exc)
                    print(f"  ! failed to link Asana task: {detail}")

            created += 1
            time.sleep(0.5)  # stay under GitHub's secondary rate limits
            continue

        # Project-only mode: draft issues (no GitHub issue number to write back).
        if gid in existing_draft_gids:
            skipped += 1
            continue

        if dry_run:
            print(f"[dry-run] would create draft project item: {name}")
            created += 1
            continue

        try:
            item = project_create_draft_issue(gh_token, project_node_id, name, body)
            print(f"Created draft item {item['id']}: {name}")
        except requests.HTTPError as exc:
            print(f"  ! failed to create '{name}': {_http_error_detail(exc)}")
            continue
        except RuntimeError as exc:
            print(f"  ! failed to create '{name}': {exc}")
            continue

        created += 1
        time.sleep(0.5)

    verb = "Would create" if dry_run else "Created"
    print(f"\nDone. {verb} {created} item(s), skipped {skipped}, Asana link updates {linked}.")


if __name__ == "__main__":
    main()
