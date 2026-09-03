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

import re
import sys
import time

import requests

from cfg import cfg
from log import configure_logging, get_logger

configure_logging()
logger = get_logger(__name__, __file__)

ASANA_BASE = "https://app.asana.com/api/1.0"
GITHUB_API = "https://api.github.com"
GITHUB_GRAPHQL = "https://api.github.com/graphql"

MARKER_TEMPLATE = "<!-- asana-task-gid:{gid} -->"
MARKER_RE = re.compile(r"<!-- asana-task-gid:(\d+) -->")
DEFAULT_GITHUB_ISSUE_FIELD_NAME = "GitHub Issue #"


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
    """Return (login, errors) after probing GITHUB_TOKEN."""
    try:
        resp = requests.get(f"{GITHUB_API}/user", headers=_gh_headers(token), timeout=30)
    except requests.RequestException as exc:
        return None, [f"Could not reach GitHub to validate GITHUB_TOKEN: {exc}"]

    if resp.status_code == 401:
        return None, [
            "GITHUB_TOKEN is invalid or expired. Create a personal access token "
            "with the `project` scope (and `repo` if using GITHUB_REPO), then set GITHUB_TOKEN."
        ]
    if resp.status_code == 403:
        return None, [
            "GITHUB_TOKEN was rejected (HTTP 403). Check that the token has the "
            f"`project` scope, and authorize it for org SSO if needed. GitHub said: {_github_error_detail(resp)}"
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
        return [
            f"GITHUB_TOKEN for {login} cannot access repository {repo}. "
            "Check GITHUB_REPO is 'owner/repo', that the token has the `repo` scope "
            "(or Issues write on a fine-grained PAT), and that org SSO is authorized if required."
        ]
    if not resp.ok:
        return [f"Failed to read GitHub repository {repo} (HTTP {resp.status_code}): {_github_error_detail(resp)}"]

    perms = resp.json().get("permissions") or {}
    if perms and not (perms.get("push") or perms.get("admin") or perms.get("maintain")):
        return [
            f"GITHUB_TOKEN for {login} can see {repo} but cannot create issues "
            "(no write access). Grant write access or the `repo` scope."
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


def validate_environment() -> str | None:
    """
    Confirm ASANA_TOKEN and GITHUB_TOKEN can access the configured projects.

    Exits with a combined error if any check fails. Returns the GitHub Projects v2
    node id when a board is configured, otherwise None.
    """
    repo = cfg.github_repo
    project_owner = cfg.github_project_owner
    project_number = cfg.github_project_number
    if not repo and not (project_owner and project_number):
        sys.exit(
            "Configure a target: set GITHUB_PROJECT_OWNER + GITHUB_PROJECT_NUMBER "
            "(project draft issues), and/or GITHUB_REPO (repository issues)."
        )

    errors: list[str] = []
    errors.extend(_check_asana_access(cfg.asana_token, cfg.asana_project_gid))

    login, token_errors = _check_github_token(cfg.github_token)
    errors.extend(token_errors)

    project_node_id = None
    if login:
        if repo:
            errors.extend(_check_github_repo(cfg.github_token, repo, login))
        if project_owner and project_number:
            project_node_id, project_errors = _check_github_project(
                cfg.github_token, project_owner, project_number, login
            )
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
    logger.info(f"Starting with log level {cfg.log_level}.")
    asana_token = cfg.asana_token
    project_gid = cfg.asana_project_gid
    gh_token = cfg.github_token

    repo = cfg.github_repo
    project_owner = cfg.github_project_owner
    project_number = cfg.github_project_number
    project_node_id = validate_environment()

    sync_completed = cfg.sync_completed
    dry_run = cfg.dry_run
    labels = cfg.default_labels

    # Project-only mode (no repo): create draft issues on the board.
    # Repo mode: create real issues; project board is an optional extra link.
    project_only = not repo
    if project_owner and project_number:
        if project_only:
            logger.info(f"Creating draft issues on Projects v2 board {project_owner}/#{project_number}.")
        else:
            logger.info(f"Linking new issues to Projects v2 board {project_owner}/#{project_number}.")

    github_issue_field_gid = None
    if not project_only:
        github_issue_field_gid = cfg.asana_github_issue_field_gid
        if not github_issue_field_gid:
            github_issue_field_gid = asana_find_custom_field_gid(
                asana_token, project_gid, DEFAULT_GITHUB_ISSUE_FIELD_NAME
            )
        if github_issue_field_gid:
            logger.info(
                f"Writing issue numbers back to Asana field "
                f"{DEFAULT_GITHUB_ISSUE_FIELD_NAME!r} ({github_issue_field_gid})."
            )
        else:
            logger.warning(
                f"Warning: Asana custom field {DEFAULT_GITHUB_ISSUE_FIELD_NAME!r} "
                "not found; issue numbers will not be written back."
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
                        logger.info(f"Existing #{issue['number']}: {name} -> {status}")
                        if status != "already linked":
                            linked += 1
                    except (requests.HTTPError, RuntimeError) as exc:
                        detail = _http_error_detail(exc) if isinstance(exc, requests.HTTPError) else str(exc)
                        logger.error(f"  ! failed to link Asana task for '{name}': {detail}")
                else:
                    logger.info(f"Existing #{issue['number']}: {name} (no Asana field to update)")
                skipped += 1
                continue

            if dry_run:
                logger.info(f"[dry-run] would create issue: {name}")
                if github_issue_field_gid:
                    logger.info("  [dry-run] would set GitHub Issue # and attach issue URL")
                created += 1
                continue

            try:
                issue = github_create_issue(gh_token, repo, name, body, labels)
            except requests.HTTPError as exc:
                logger.error(f"  ! failed to create '{name}': {_http_error_detail(exc)}")
                continue

            logger.info(f"Created #{issue['number']}: {name} -> {issue['html_url']}")
            if project_node_id:
                try:
                    add_issue_to_project(gh_token, project_node_id, issue["node_id"])
                except Exception as exc:  # noqa: BLE001 - report and continue
                    logger.error(f"  ! could not add to project board: {exc}")

            if github_issue_field_gid:
                try:
                    status = reconcile_asana_github_link(
                        asana_token,
                        task,
                        issue,
                        github_issue_field_gid,
                        dry_run=False,
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
