"""Permanently delete every issue in a GitHub repository.

The GitHub REST API cannot delete issues. This script lists issues over REST,
then removes them with the GraphQL ``deleteIssue`` mutation. The token must
belong to a repo admin (classic PAT ``repo`` scope, or a fine-grained PAT with
Issues: write plus admin rights). Pull requests are left alone.
"""

from __future__ import annotations

import argparse
import sys

import requests

from asana_sync.cfg import cfg
from asana_sync.log import configure_logging, get_logger

configure_logging()
logger = get_logger(__name__, __file__)

GITHUB_API = "https://api.github.com"
GITHUB_GRAPHQL = "https://api.github.com/graphql"

DELETE_ISSUE_MUTATION = """
mutation($id: ID!) {
  deleteIssue(input: {issueId: $id}) {
    repository { nameWithOwner }
  }
}
"""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Permanently delete all issues in a GitHub repository (not pull requests)."
    )
    parser.add_argument(
        "repo",
        nargs="?",
        help='Target repository as "owner/repo"',
    )
    parser.add_argument(
        "--repo",
        "-r",
        dest="repo_flag",
        help='Target repository as "owner/repo" (alternative to positional)',
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List issues that would be deleted without deleting them",
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Skip the confirmation prompt",
    )
    return parser.parse_args(argv)


def list_repo_issues(token: str, repo: str) -> list[dict]:
    """Return every issue in the repo, excluding pull requests."""
    issues: list[dict] = []
    page = 1
    while True:
        resp = requests.get(
            f"{GITHUB_API}/repos/{repo}/issues",
            headers=_gh_headers(token),
            params={"state": "all", "per_page": 100, "page": page},
            timeout=30,
        )
        if not resp.ok:
            sys.exit(f"Failed to list issues in {repo} (HTTP {resp.status_code}): {_github_error_detail(resp)}")
        batch = resp.json()
        if not batch:
            break
        for issue in batch:
            if "pull_request" in issue:
                continue
            issues.append(issue)
        page += 1
    return issues


def delete_issue(token: str, node_id: str) -> None:
    """Permanently delete one issue by GraphQL node id."""
    resp = requests.post(
        GITHUB_GRAPHQL,
        headers=_gh_headers(token),
        json={"query": DELETE_ISSUE_MUTATION, "variables": {"id": node_id}},
        timeout=30,
    )
    if not resp.ok:
        raise RuntimeError(f"HTTP {resp.status_code}: {_github_error_detail(resp)}")
    payload = resp.json()
    errors = payload.get("errors") or []
    if errors:
        messages = "; ".join(err.get("message") or str(err) for err in errors)
        raise RuntimeError(messages)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    repo = _resolve_repo(args)
    token = cfg.github_token

    issues = list_repo_issues(token, repo)
    if not issues:
        logger.info(f"No issues found in {repo}.")
        return

    logger.info(f"Found {len(issues)} issue(s) in {repo}.")
    for issue in issues:
        logger.info(f"  #{issue['number']} {issue.get('title') or ''}")

    if args.dry_run:
        logger.info("Dry run: nothing deleted.")
        return

    if not args.yes and not _confirm_delete(repo, len(issues)):
        sys.exit("Aborted.")

    deleted = failed = 0
    for issue in issues:
        number = issue["number"]
        node_id = issue.get("node_id")
        if not node_id:
            logger.error(f"#{number} has no node_id; skipping.")
            failed += 1
            continue
        try:
            delete_issue(token, node_id)
        except (requests.RequestException, RuntimeError) as exc:
            logger.error(f"Failed to delete #{number}: {exc}")
            failed += 1
            continue
        deleted += 1
        logger.info(f"Deleted #{number}.")

    logger.info(f"Done. Deleted {deleted}, failed {failed}.")
    if failed:
        sys.exit(1)


def _gh_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _github_error_detail(resp: requests.Response) -> str:
    try:
        payload = resp.json()
    except ValueError:
        payload = {}
    message = payload.get("message")
    if message:
        return message
    return resp.text.strip() or resp.reason or f"HTTP {resp.status_code}"


def _resolve_repo(args: argparse.Namespace) -> str:
    repo = args.repo_flag or args.repo
    if not repo:
        sys.exit('Pass the repository as "owner/repo" (positional or --repo).')
    if args.repo_flag and args.repo and args.repo_flag != args.repo:
        sys.exit(f"Conflicting repos: {args.repo!r} and --repo {args.repo_flag!r}.")
    if "/" not in repo or repo.count("/") != 1:
        sys.exit(f'Repository must be "owner/repo", got: {repo!r}')
    owner, name = repo.split("/")
    if not owner or not name:
        sys.exit(f'Repository must be "owner/repo", got: {repo!r}')
    return repo


def _confirm_delete(repo: str, count: int) -> bool:
    prompt = f"Permanently delete {count} issue(s) from {repo}? [y/N] "
    try:
        answer = input(prompt)
    except EOFError:
        return False
    return answer.strip().casefold() in {"y", "yes"}


if __name__ == "__main__":
    main()
