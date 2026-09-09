#!/usr/bin/env python3
"""
asana_to_github.py

Pull tasks from one Asana project and create GitHub repository issues
(or optional Projects draft issues).

Key properties:
- Idempotent: each item body carries a hidden marker with the Asana task GID.
  On re-run, tasks that already have an item are skipped, so you can run this
  repeatedly (cron, CI, manually) without creating duplicates.
- Default destination: real issues in GITHUB_REPO. A Projects v2 board is an
  optional extra link.
- Optional: create draft issues on a Projects v2 board when no repo is set.
- When using GITHUB_REPO: write the GitHub issue number back to Asana's
  "GitHub Issue #" or "GitHub Issue" custom field and attach the issue URL
  on the task. Already-synced tasks are reconciled on every run: title and
  body are updated when the Asana task has changed, and Asana comments are
  copied onto the GitHub issue (idempotent via a story-gid marker).
- Optional import gate: only create issues for tasks in named Asana sections,
  or whose custom field matches those names.

Setup:
    uv sync

Required environment variables:
    ASANA_TOKEN             Asana personal access token
    ASANA_PROJECT_GID       The project GID to pull tasks from
                            (the number in the project URL: .../0/<GID>/list)
                            CLI: --project-gid
    GITHUB_TOKEN            GitHub PAT with `project` scope (add `repo` if using
                            GITHUB_REPO below)

Target (one of):
    GITHUB_REPO             Target repo as "owner/repo" (creates real issues)
                            CLI: --repo
  or:
    GITHUB_PROJECT_OWNER    Org or user login that owns the Projects v2 board
    GITHUB_PROJECT_NUMBER   The project number (the integer in the project URL)

Optional environment variables:
    ASANA_SECTION           Only import tasks in these Asana sections
                            (comma-separated). CLI: --section
    ASANA_STATUS_FIELD      Match ASANA_SECTION against this custom field
                            instead of a board section. CLI: --status-field
    ASANA_GITHUB_ISSUE_FIELD_GID
                            Custom field GID for "GitHub Issue #" or
                            "GitHub Issue". If unset, the field is resolved by
                            name on the Asana project.
    SYNC_COMPLETED          "true" to also sync completed tasks (default: skip them)
                            CLI: --sync-completed
    DEFAULT_LABELS          Comma-separated labels for repo issues only
    DRY_RUN                 "true" to preview without creating anything
                            CLI: --dry-run
    A2G_ENRICHMENT          Post-create hook (`off` by default). Other values
                            log and do nothing until a provider is added.

Usage:
    uv run A2G --repo owner/repo --section "In Progress"
    uv run A2G --section "In Progress" --status-field Status
"""

import argparse
import re
import sys
import time

import requests

from asana_sync.asana_columns import match_named_value, parse_section_names, resolve_sections
from asana_sync.cfg import cfg, normalize_asana_gid
from asana_sync.log import configure_logging, get_logger

configure_logging()
logger = get_logger(__name__, __file__)

ASANA_BASE = "https://app.asana.com/api/1.0"
GITHUB_API = "https://api.github.com"
GITHUB_GRAPHQL = "https://api.github.com/graphql"

MARKER_TEMPLATE = "<!-- asana-task-gid:{gid} -->"
MARKER_RE = re.compile(r"<!-- asana-task-gid:(\d+) -->")
STORY_MARKER_TEMPLATE = "<!-- asana-story-gid:{gid} -->"
STORY_MARKER_RE = re.compile(r"<!-- asana-story-gid:(\d+) -->")
DEFAULT_GITHUB_ISSUE_FIELD_NAME = "GitHub Issue #"
GITHUB_ISSUE_FIELD_NAMES = (DEFAULT_GITHUB_ISSUE_FIELD_NAME, "GitHub Issue")
TASK_OPT_FIELDS = (
    "name,notes,completed,permalink_url,assignee.name,due_on,"
    "custom_fields,custom_fields.name,custom_fields.gid,"
    "custom_fields.text_value,custom_fields.display_value"
)


def _asana_headers(token):
    return {"Authorization": f"Bearer {token}"}


def _asana_paginated_tasks(token, url):
    """Yield tasks from an Asana collection URL, following pagination."""
    headers = _asana_headers(token)
    params = {"opt_fields": TASK_OPT_FIELDS, "limit": 100}
    while url:
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        payload = resp.json()
        yield from payload.get("data", [])
        next_page = payload.get("next_page")
        if next_page and next_page.get("uri"):
            url = next_page["uri"]
            params = None
        else:
            url = None


def asana_get_tasks(token, project_gid):
    """Yield every task in a project, following pagination."""
    yield from _asana_paginated_tasks(token, f"{ASANA_BASE}/projects/{project_gid}/tasks")


def asana_list_sections(token, project_gid):
    """Return sections (columns) for an Asana project."""
    resp = requests.get(
        f"{ASANA_BASE}/projects/{project_gid}/sections",
        headers=_asana_headers(token),
        params={"opt_fields": "name,gid"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("data", [])


def asana_get_section_tasks(token, section_gid):
    """Yield every task in an Asana section, following pagination."""
    yield from _asana_paginated_tasks(token, f"{ASANA_BASE}/sections/{section_gid}/tasks")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Pull Asana tasks into GitHub issues or Projects.")
    parser.add_argument(
        "--section",
        help="Only import tasks in this Asana section (comma-separated). Env: ASANA_SECTION",
    )
    parser.add_argument(
        "--status-field",
        help="Match --section against this custom field instead of a board section. Env: ASANA_STATUS_FIELD",
    )
    parser.add_argument("--repo", help="Target repository as owner/repo. Env: GITHUB_REPO")
    parser.add_argument("--project-gid", help="Asana project GID. Env: ASANA_PROJECT_GID")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing. Env: DRY_RUN")
    parser.add_argument(
        "--sync-completed",
        action="store_true",
        help="Also import completed Asana tasks. Env: SYNC_COMPLETED",
    )
    return parser.parse_args(argv)


def asana_custom_field_settings(token, project_gid):
    """Return custom field settings for a project."""
    resp = requests.get(
        f"{ASANA_BASE}/projects/{project_gid}/custom_field_settings",
        headers=_asana_headers(token),
        params={"opt_fields": "custom_field.name,custom_field.gid"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("data", [])


def asana_find_custom_field_gid(token, project_gid, field_name=DEFAULT_GITHUB_ISSUE_FIELD_NAME):
    """Resolve a project custom field GID by display name."""
    for setting in asana_custom_field_settings(token, project_gid):
        field = setting.get("custom_field") or {}
        if field.get("name") == field_name:
            return field.get("gid")
    return None


def asana_find_github_issue_field(token, project_gid):
    """Return (gid, name) for GitHub Issue # or GitHub Issue, else (None, None)."""
    by_name = {}
    for setting in asana_custom_field_settings(token, project_gid):
        field = setting.get("custom_field") or {}
        name = field.get("name")
        if name:
            by_name[name] = field.get("gid")
    for name in GITHUB_ISSUE_FIELD_NAMES:
        gid = by_name.get(name)
        if gid:
            return gid, name
    return None, None


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


def asana_list_comment_stories(token, task_gid):
    """Return Asana comment stories on a task, following pagination."""
    headers = _asana_headers(token)
    url = f"{ASANA_BASE}/tasks/{task_gid}/stories"
    params = {
        "opt_fields": "gid,text,created_at,created_by.name,resource_subtype,type",
        "limit": 100,
    }
    stories = []
    while url:
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        payload = resp.json()
        for story in payload.get("data", []):
            subtype = story.get("resource_subtype")
            if subtype != "comment_added" and story.get("type") != "comment":
                continue
            if not (story.get("text") or "").strip():
                continue
            stories.append(story)
        next_page = payload.get("next_page")
        if next_page and next_page.get("uri"):
            url = next_page["uri"]
            params = None
        else:
            url = None
    return stories


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

    The issue URL is attached even when no custom field GID is available.
    Returns a short status string describing what happened.
    """
    task_gid = task["gid"]
    issue_number = issue["number"]
    issue_url = issue["html_url"]
    title = issue.get("title") or task.get("name") or f"Issue #{issue_number}"
    actions = []

    if field_gid:
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


def _asana_error_detail(resp: requests.Response) -> str:
    try:
        errors = resp.json().get("errors") or []
    except ValueError:
        errors = []
    messages = [err.get("message") for err in errors if err.get("message")]
    if messages:
        return "; ".join(messages)
    return resp.text.strip() or resp.reason or f"HTTP {resp.status_code}"


def _github_error_detail(resp: requests.Response) -> str:
    try:
        payload = resp.json()
    except ValueError:
        payload = {}
    message = payload.get("message")
    if message:
        return message
    return resp.text.strip() or resp.reason or f"HTTP {resp.status_code}"


def _check_asana_access(token: str, project_gid: str) -> list[str]:
    """Return error strings if ASANA_TOKEN cannot use the configured project."""
    headers = _asana_headers(token)
    try:
        me = requests.get(
            f"{ASANA_BASE}/users/me",
            headers=headers,
            params={"opt_fields": "name"},
            timeout=30,
        )
    except requests.RequestException as exc:
        return [f"Could not reach Asana to validate ASANA_TOKEN: {exc}"]

    if me.status_code == 401:
        return [
            "ASANA_TOKEN is invalid or expired. Create a new personal access token "
            "at https://app.asana.com/0/my-apps and set ASANA_TOKEN."
        ]
    if me.status_code == 403:
        return [
            "ASANA_TOKEN was rejected (HTTP 403). The token may lack permission "
            f"or have been revoked. Asana said: {_asana_error_detail(me)}"
        ]
    if not me.ok:
        return [f"Asana rejected ASANA_TOKEN (HTTP {me.status_code}): {_asana_error_detail(me)}"]

    user_name = (me.json().get("data") or {}).get("name") or "the authenticated user"

    try:
        project = requests.get(
            f"{ASANA_BASE}/projects/{project_gid}",
            headers=headers,
            params={"opt_fields": "name"},
            timeout=30,
        )
    except requests.RequestException as exc:
        return [f"Could not reach Asana to validate ASANA_PROJECT_GID: {exc}"]

    if project.status_code in {403, 404}:
        return [
            f"ASANA_TOKEN for {user_name} cannot access Asana project {project_gid}. "
            "Check ASANA_PROJECT_GID (the number in the project URL) and that this "
            "user is a member of that project."
        ]
    if not project.ok:
        return [
            f"Failed to read Asana project {project_gid} (HTTP {project.status_code}): {_asana_error_detail(project)}"
        ]

    project_name = (project.json().get("data") or {}).get("name") or project_gid
    logger.info(f"Asana: {user_name} can access project {project_name!r} ({project_gid}).")
    return []


def _check_github_token(token: str) -> tuple[str | None, list[str]]:
    """Return (login, errors) after probing the GitHub token."""
    if cfg.using_actions_job_token():
        # The job token is a GitHub App installation token. GET /user returns
        # 403 Resource not accessible by integration — skip that probe.
        return "github-actions", []

    try:
        resp = requests.get(f"{GITHUB_API}/user", headers=_gh_headers(token), timeout=30)
    except requests.RequestException as exc:
        return None, [f"Could not reach GitHub to validate GITHUB_TOKEN: {exc}"]

    if resp.status_code == 401:
        return None, [
            "GITHUB_TOKEN is invalid or expired. Create a personal access token "
            "and set GITHUB_TOKEN locally, or secret GH_TOKEN in Actions."
        ]
    if resp.status_code == 403:
        return None, [
            "GITHUB_TOKEN was rejected (HTTP 403). Authorize the token for org SSO "
            f"if needed. GitHub said: {_github_error_detail(resp)}"
        ]
    if not resp.ok:
        return None, [f"GitHub rejected GITHUB_TOKEN (HTTP {resp.status_code}): {_github_error_detail(resp)}"]

    login = resp.json().get("login") or "the authenticated user"
    return login, []


def _check_github_repo(token: str, repo: str, login: str) -> list[str]:
    try:
        resp = requests.get(f"{GITHUB_API}/repos/{repo}", headers=_gh_headers(token), timeout=30)
    except requests.RequestException as exc:
        return [f"Could not reach GitHub to validate GITHUB_REPO={repo}: {exc}"]

    if resp.status_code in {403, 404}:
        extra = (
            " Grant issues: write on the workflow."
            if cfg.using_actions_job_token()
            else " Check GITHUB_REPO is 'owner/repo', that the token can read this repository, "
            "and that org SSO is authorized if required."
        )
        return [f"GITHUB_TOKEN for {login} cannot access repository {repo}.{extra}"]
    if not resp.ok:
        return [f"Failed to read GitHub repository {repo} (HTTP {resp.status_code}): {_github_error_detail(resp)}"]

    perms = resp.json().get("permissions") or {}
    # Job tokens with issues: write often have pull but not push. Do not require push.
    if (
        not cfg.using_actions_job_token()
        and perms
        and not (perms.get("push") or perms.get("admin") or perms.get("maintain") or perms.get("triage"))
        and perms.get("pull")
    ):
        return [
            f"GITHUB_TOKEN for {login} can see {repo} but cannot create issues "
            "(no write access). Grant Issues write or the `repo` scope."
        ]

    logger.info(f"GitHub: {login} can access repository {repo}.")
    return []


def _check_github_project(token: str, owner: str, number: int, login: str) -> tuple[str | None, list[str]]:
    """Return (project_node_id, errors) for the configured Projects v2 board."""
    last_errors: list[str] = []
    for kind in ("organization", "user"):
        query = (
            f"query($owner:String!, $number:Int!){{ {kind}(login:$owner){{ "
            f"projectV2(number:$number){{ id title viewerCanUpdate }} }} }}"
        )
        try:
            resp = requests.post(
                GITHUB_GRAPHQL,
                headers={"Authorization": f"Bearer {token}"},
                json={"query": query, "variables": {"owner": owner, "number": number}},
                timeout=30,
            )
        except requests.RequestException as exc:
            return None, [f"Could not reach GitHub to validate the Projects v2 board: {exc}"]

        if resp.status_code == 401:
            return None, [
                "GITHUB_TOKEN is invalid or expired when calling GraphQL. "
                "Create a new PAT with the `project` scope and set GITHUB_TOKEN."
            ]
        if resp.status_code == 403:
            return None, [
                "GITHUB_TOKEN cannot query GitHub Projects (HTTP 403). "
                f"Grant the `project` scope and authorize org SSO if needed. GitHub said: {_github_error_detail(resp)}"
            ]
        if not resp.ok:
            return None, [
                f"GitHub GraphQL rejected the project lookup (HTTP {resp.status_code}): {_github_error_detail(resp)}"
            ]

        payload = resp.json()
        gql_errors = payload.get("errors") or []
        holder = (payload.get("data") or {}).get(kind)
        project = (holder or {}).get("projectV2") if holder else None
        if project:
            title = project.get("title") or f"{owner}/#{number}"
            if project.get("viewerCanUpdate") is False:
                return None, [
                    f"GITHUB_TOKEN for {login} can see Projects v2 board {owner}/#{number} "
                    f"({title!r}) but cannot add items. Grant write access on that board "
                    "and the `project` scope on the token."
                ]
            logger.info(f"GitHub: {login} can access Projects v2 board {owner}/#{number} ({title!r}).")
            return project["id"], []

        messages = [err.get("message") or "" for err in gql_errors]
        if any(
            err.get("type") == "FORBIDDEN" or "not accessible" in (err.get("message") or "").lower()
            for err in gql_errors
        ):
            return None, [
                f"GITHUB_TOKEN for {login} cannot access Projects v2 board {owner}/#{number}. "
                "Classic PATs need the `project` scope; fine-grained PATs need Projects read/write. "
                f"GitHub said: {'; '.join(m for m in messages if m)}"
            ]
        last_errors.extend(m for m in messages if m and "Could not resolve to an Organization" not in m)

    extra = f" GitHub said: {'; '.join(last_errors)}" if last_errors else ""
    return None, [
        f"GITHUB_TOKEN for {login} cannot access Projects v2 board {owner}/#{number}. "
        "Check GITHUB_PROJECT_OWNER and GITHUB_PROJECT_NUMBER, that the token has the "
        f"`project` scope, and that the user can see that board.{extra}"
    ]


def validate_environment(repo: str | None, project_gid: str) -> str | None:
    """
    Confirm ASANA_TOKEN and GITHUB_TOKEN can access the configured projects.

    Exits with a combined error if any check fails. Returns the GitHub Projects v2
    node id when a board is configured, otherwise None.
    """
    project_owner = cfg.github_project_owner
    project_number = cfg.github_project_number
    if not repo and not (project_owner and project_number):
        sys.exit(
            "Configure a target: set GITHUB_PROJECT_OWNER + GITHUB_PROJECT_NUMBER "
            "(project draft issues), and/or GITHUB_REPO (repository issues)."
        )

    gh_token = cfg.require_github_token(repo)

    errors: list[str] = []
    errors.extend(_check_asana_access(cfg.asana_token, project_gid))

    login, token_errors = _check_github_token(gh_token)
    errors.extend(token_errors)

    project_node_id = None
    if login:
        if repo:
            errors.extend(_check_github_repo(gh_token, repo, login))
        if project_owner and project_number:
            project_node_id, project_errors = _check_github_project(gh_token, project_owner, project_number, login)
            errors.extend(project_errors)

    if errors:
        sys.exit("Environment validation failed:\n- " + "\n- ".join(errors))

    return project_node_id


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
                    "body": body,
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


def github_update_issue(token, repo, number, *, title=None, body=None):
    data = {}
    if title is not None:
        data["title"] = title
    if body is not None:
        data["body"] = body
    if not data:
        return None
    resp = requests.patch(
        f"{GITHUB_API}/repos/{repo}/issues/{number}",
        headers=_gh_headers(token),
        json=data,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def github_list_issue_comments(token, repo, number):
    """Return all comments on a GitHub issue, following pagination."""
    comments = []
    page = 1
    while True:
        resp = requests.get(
            f"{GITHUB_API}/repos/{repo}/issues/{number}/comments",
            headers=_gh_headers(token),
            params={"per_page": 100, "page": page},
            timeout=30,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        comments.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return comments


def github_create_issue_comment(token, repo, number, body):
    resp = requests.post(
        f"{GITHUB_API}/repos/{repo}/issues/{number}/comments",
        headers=_gh_headers(token),
        json={"body": body},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


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


def iter_import_tasks(token, project_gid, wanted_names, status_field):
    """Yield tasks that pass the section gate, or every project task if ungated."""
    if not wanted_names or status_field:
        yield from asana_get_tasks(token, project_gid)
        return

    sections = asana_list_sections(token, project_gid)
    resolved, missing = resolve_sections(wanted_names, sections)
    if missing:
        available = ", ".join(repr(section.get("name")) for section in sections) or "(none)"
        sys.exit(f"No Asana section matched {missing!r}. Sections on this project: {available}")

    seen: set[str] = set()
    for section in resolved:
        logger.info(f"Importing from section {section['name']!r} ({section['gid']}).")
        for task in asana_get_section_tasks(token, section["gid"]):
            gid = task["gid"]
            if gid in seen:
                continue
            seen.add(gid)
            yield task


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


def build_github_comment_from_story(story):
    """Format an Asana comment story as a GitHub issue comment body."""
    author = (story.get("created_by") or {}).get("name") or "Asana"
    created = (story.get("created_at") or "")[:10]
    text = (story.get("text") or "").strip()
    header = f"**{author}** commented in Asana"
    if created:
        header += f" ({created})"
    header += ":"
    return f"{header}\n\n{text}\n\n{STORY_MARKER_TEMPLATE.format(gid=story['gid'])}"


def synced_asana_story_gids(comments):
    """Return Asana story GIDs already present on GitHub comments."""
    gids = set()
    for comment in comments:
        gids.update(STORY_MARKER_RE.findall(comment.get("body") or ""))
    return gids


def reconcile_github_issue_from_asana(token, repo, task, issue, *, dry_run=False):
    """
    Update the GitHub issue title and body when they differ from the Asana task.

    Returns a list of short action strings (empty when already in sync).
    """
    actions = []
    desired_title = task.get("name") or "(untitled Asana task)"
    desired_body = build_body(task)
    patch = {}
    if (issue.get("title") or "") != desired_title:
        patch["title"] = desired_title
        actions.append(f"update title to {desired_title!r}")
    if (issue.get("body") or "").strip() != desired_body.strip():
        patch["body"] = desired_body
        actions.append("update body from Asana")
    if patch and not dry_run:
        github_update_issue(token, repo, issue["number"], **patch)
        issue.update(patch)
    return actions


def sync_asana_comments_to_github(asana_token, gh_token, repo, task, issue, *, dry_run=False):
    """
    Copy Asana task comments onto the GitHub issue when they are not already there.

    Returns a short status string, or None when there is nothing to copy.
    """
    stories = asana_list_comment_stories(asana_token, task["gid"])
    if not stories:
        return None

    existing = github_list_issue_comments(gh_token, repo, issue["number"])
    already = synced_asana_story_gids(existing)
    pending = [story for story in stories if story["gid"] not in already]
    if not pending:
        return None

    if dry_run:
        return f"would copy {len(pending)} comment(s)"

    copied = 0
    for story in pending:
        github_create_issue_comment(
            gh_token,
            repo,
            issue["number"],
            build_github_comment_from_story(story),
        )
        copied += 1
        time.sleep(0.25)
    return f"copied {copied} comment(s)"


def reconcile_existing_issue(
    asana_token,
    gh_token,
    repo,
    task,
    issue,
    field_gid,
    *,
    dry_run=False,
    update_content=True,
):
    """
    Reconcile an existing GitHub issue with its Asana task.

    Updates title/body when they drifted, writes the issue number and URL
    back to Asana, and copies missing Asana comments onto the issue.
    """
    actions = []
    if update_content:
        actions.extend(reconcile_github_issue_from_asana(gh_token, repo, task, issue, dry_run=dry_run))
    link_status = reconcile_asana_github_link(
        asana_token,
        task,
        issue,
        field_gid,
        dry_run=dry_run,
    )
    if link_status != "already linked":
        actions.append(link_status)
    comment_status = sync_asana_comments_to_github(
        asana_token,
        gh_token,
        repo,
        task,
        issue,
        dry_run=dry_run,
    )
    if comment_status:
        actions.append(comment_status)
    if not actions:
        return "already linked"
    return ", ".join(actions)


def enrich_created_issue(token, repo, issue, task, *, mode=None):
    """Optional post-create hook. Default mode ``off`` is a no-op (no extra HTTP)."""
    chosen = (mode if mode is not None else cfg.a2g_enrichment) or "off"
    if chosen == "off":
        return
    logger.info(
        f"Enrichment mode {chosen!r} is not implemented; skipping "
        f"#{issue.get('number')} for Asana task {task.get('gid')}."
    )


def main(argv=None):
    args = parse_args(argv)
    logger.info(f"Starting with log level {cfg.log_level}.")
    asana_token = cfg.asana_token
    project_gid = normalize_asana_gid(
        args.project_gid or cfg.asana_project_gid,
        name="--project-gid" if args.project_gid else "ASANA_PROJECT_GID",
    )
    gh_token = cfg.github_token

    repo = args.repo or cfg.github_repo
    if repo and "/" not in repo:
        sys.exit(f'GITHUB_REPO must be "owner/repo", got: {repo!r}')

    project_owner = cfg.github_project_owner
    project_number = cfg.github_project_number
    project_node_id = validate_environment(repo, project_gid)

    sync_completed = args.sync_completed or cfg.sync_completed
    dry_run = args.dry_run or cfg.dry_run
    labels = cfg.default_labels
    section_raw = args.section or cfg.asana_section
    status_field = args.status_field or cfg.asana_status_field
    wanted_names = parse_section_names(section_raw)

    if status_field and not wanted_names:
        sys.exit("--status-field requires --section (or ASANA_SECTION) with the value to match")

    status_field_gid = None
    if status_field:
        status_field_gid = asana_find_custom_field_gid(asana_token, project_gid, status_field)
        if not status_field_gid:
            sys.exit(f"Asana custom field {status_field!r} was not found on project {project_gid}")
        logger.info(f"Import gate: custom field {status_field!r} in {wanted_names}")
    elif wanted_names:
        logger.info(f"Import gate: Asana section(s) {wanted_names}")
    else:
        logger.info("Import gate: none (all incomplete tasks)")

    # Project-only mode (no repo): create draft issues on the board.
    # Repo mode: create real issues; project board is an optional extra link.
    project_only = not repo
    if project_owner and project_number:
        if project_only:
            logger.info(f"Creating draft issues on Projects v2 board {project_owner}/#{project_number}.")
        else:
            logger.info(f"Linking new issues to Projects v2 board {project_owner}/#{project_number}.")

    github_issue_field_gid = None
    github_issue_field_name = DEFAULT_GITHUB_ISSUE_FIELD_NAME
    if not project_only:
        github_issue_field_gid = cfg.asana_github_issue_field_gid
        if not github_issue_field_gid:
            github_issue_field_gid, github_issue_field_name = asana_find_github_issue_field(asana_token, project_gid)
        if github_issue_field_gid:
            logger.info(
                f"Writing issue numbers back to Asana field {github_issue_field_name!r} ({github_issue_field_gid})."
            )
        else:
            logger.warning(
                "Asana custom field 'GitHub Issue #' / 'GitHub Issue' was not found; "
                "issue numbers will not be written back. The issue URL will still be attached."
            )

    logger.info("Loading existing items to avoid duplicates...")
    existing_issues = {}
    existing_draft_gids = set()
    if dry_run and project_only:
        pass
    elif project_only:
        existing_draft_gids = project_existing_synced_gids(gh_token, project_node_id)
    else:
        existing_issues = github_existing_synced_issues(gh_token, repo)
    logger.info(f"Found {len(existing_issues) or len(existing_draft_gids)} already-synced task(s).")

    created = skipped = linked = 0
    for task in iter_import_tasks(asana_token, project_gid, wanted_names, status_field):
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
                try:
                    status = reconcile_existing_issue(
                        asana_token,
                        gh_token,
                        repo,
                        task,
                        issue,
                        github_issue_field_gid,
                        dry_run=dry_run,
                    )
                    logger.info(f"Existing #{issue['number']}: {name} -> {status}")
                    if status != "already linked":
                        linked += 1
                except (requests.HTTPError, RuntimeError) as exc:
                    detail = _http_error_detail(exc) if isinstance(exc, requests.HTTPError) else str(exc)
                    logger.error(f"  ! failed to reconcile '{name}': {detail}")
                skipped += 1
                continue

            if status_field_gid and not match_named_value(task_custom_field_text(task, status_field_gid), wanted_names):
                skipped += 1
                continue

            if dry_run:
                logger.info(f"[dry-run] would create issue: {name}")
                if github_issue_field_gid:
                    logger.info("  [dry-run] would set GitHub Issue # and attach issue URL")
                else:
                    logger.info("  [dry-run] would attach issue URL")
                created += 1
                continue

            try:
                issue = github_create_issue(gh_token, repo, name, body, labels)
            except requests.HTTPError as exc:
                logger.error(f"  ! failed to create '{name}': {_http_error_detail(exc)}")
                continue

            logger.info(f"Created #{issue['number']}: {name} -> {issue['html_url']}")
            enrich_created_issue(gh_token, repo, issue, task)
            if project_node_id:
                try:
                    add_issue_to_project(gh_token, project_node_id, issue["node_id"])
                except Exception as exc:  # noqa: BLE001 - report and continue
                    logger.error(f"  ! could not add to project board: {exc}")

            try:
                status = reconcile_existing_issue(
                    asana_token,
                    gh_token,
                    repo,
                    task,
                    issue,
                    github_issue_field_gid,
                    dry_run=False,
                    update_content=False,
                )
                logger.info(f"  Asana: {status}")
                linked += 1
            except (requests.HTTPError, RuntimeError) as exc:
                detail = _http_error_detail(exc) if isinstance(exc, requests.HTTPError) else str(exc)
                logger.error(f"  ! failed to link Asana task: {detail}")

            created += 1
            time.sleep(0.5)  # stay under GitHub's secondary rate limits
            continue

        # Project-only mode: draft issues (no GitHub issue number to write back).
        if gid in existing_draft_gids:
            skipped += 1
            continue

        if status_field_gid and not match_named_value(task_custom_field_text(task, status_field_gid), wanted_names):
            skipped += 1
            continue

        if dry_run:
            logger.info(f"[dry-run] would create draft project item: {name}")
            created += 1
            continue

        try:
            item = project_create_draft_issue(gh_token, project_node_id, name, body)
            logger.info(f"Created draft item {item['id']}: {name}")
        except requests.HTTPError as exc:
            logger.error(f"  ! failed to create '{name}': {_http_error_detail(exc)}")
            continue
        except RuntimeError as exc:
            logger.error(f"  ! failed to create '{name}': {exc}")
            continue

        created += 1
        time.sleep(0.5)

    verb = "Would create" if dry_run else "Created"
    logger.info(f"\nDone. {verb} {created} item(s), skipped {skipped}, Asana link updates {linked}.")


if __name__ == "__main__":
    main()
