#!/usr/bin/env bash
set -euo pipefail

mode="$(printf '%s' "$SYNC_MODE" | tr '[:upper:]' '[:lower:]')"
if [ "$mode" != "a2g" ] && [ "$mode" != "g2a" ]; then
  echo "::error::mode must be a2g or g2a, got: ${SYNC_MODE}"
  exit 1
fi

missing=0
if [ -z "$ASANA_TOKEN" ]; then
  echo "::error::asana_token is required"
  missing=1
fi
current_repo="${GITHUB_REPOSITORY:-}"
target_repo="${GITHUB_REPO:-$current_repo}"
using_project=false
if [ -n "${GITHUB_PROJECT_OWNER:-}" ] && [ -n "${GITHUB_PROJECT_NUMBER:-}" ]; then
  using_project=true
fi
if [ -z "${GH_TOKEN}" ]; then
  current_lc="$(printf '%s' "$current_repo" | tr '[:upper:]' '[:lower:]')"
  target_lc="$(printf '%s' "$target_repo" | tr '[:upper:]' '[:lower:]')"
  if [ "$using_project" = "true" ]; then
    echo "::error::GitHub Projects requires a PAT. Set secret GH_TOKEN."
    missing=1
  elif [ -z "$current_repo" ] || [ "$target_lc" != "$current_lc" ]; then
    echo "::error::Secret GH_TOKEN is required when the target repository is not this repository"
    missing=1
  else
    echo "Using the built-in GITHUB_TOKEN for ${target_repo} (same repository, no Projects board)."
  fi
fi
if [ -z "$ASANA_PROJECT_GID" ]; then
  echo "::error::asana_project_gid is required"
  missing=1
elif printf '%s' "$ASANA_PROJECT_GID" | grep -Eq '[eE]'; then
  echo "::error::asana_project_gid looks like a float (${ASANA_PROJECT_GID}). Quote it as a string, for example asana_project_gid: \"1210771720905224\""
  missing=1
fi

if [ "$mode" = "a2g" ]; then
  if [ -z "$GITHUB_REPO" ] && { [ -z "$GITHUB_PROJECT_OWNER" ] || [ -z "$GITHUB_PROJECT_NUMBER" ]; }; then
    echo "::error::Configure github_project_owner + github_project_number and/or github_repo"
    missing=1
  fi
  if [ "$missing" -ne 0 ]; then
    exit 1
  fi
  a2g_args=()
  if [ -n "${ASANA_SECTION}" ]; then
    a2g_args+=(--section "$ASANA_SECTION")
  fi
  if [ -n "${ASANA_STATUS_FIELD}" ]; then
    a2g_args+=(--status-field "$ASANA_STATUS_FIELD")
  fi
  if [ -n "$GITHUB_REPO" ]; then
    a2g_args+=(--repo "$GITHUB_REPO")
  fi
  if [ -n "$ASANA_PROJECT_GID" ]; then
    a2g_args+=(--project-gid "$ASANA_PROJECT_GID")
  fi
  if [ "$DRY_RUN" = "true" ]; then
    a2g_args+=(--dry-run)
  fi
  if [ "$SYNC_COMPLETED" = "true" ]; then
    a2g_args+=(--sync-completed)
  fi
  uv run A2G "${a2g_args[@]}"
  exit 0
fi

if [ -z "$GITHUB_REPO" ] && [ -n "${current_repo}" ]; then
  GITHUB_REPO="${current_repo}"
fi

if [ "$SYNC_ALL_PROJECT_ITEMS" = "true" ]; then
  if { [ -z "$GITHUB_PROJECT_OWNER" ] || [ -z "$GITHUB_PROJECT_NUMBER" ]; } && [ -z "$GITHUB_REPO" ]; then
    echo "::error::g2a requires github_repo when GitHub Projects is unset"
    missing=1
  fi
elif [ -z "$GITHUB_ISSUE_NUMBER" ]; then
  echo "::error::g2a requires issue_number, or set sync_all_project_items=true"
  missing=1
elif [ -z "$GITHUB_REPO" ]; then
  echo "::error::g2a single-item mode requires github_repo"
  missing=1
fi
if [ "$missing" -ne 0 ]; then
  exit 1
fi

args=()
if [ "$SYNC_ALL_PROJECT_ITEMS" = "true" ]; then
  args+=(--all-project-items)
  if [ -n "$GITHUB_PROJECT_OWNER" ] && [ -n "$GITHUB_PROJECT_NUMBER" ]; then
    echo "Reconciling all project items on ${GITHUB_PROJECT_OWNER}/#${GITHUB_PROJECT_NUMBER}"
  else
    echo "Reconciling all issues in ${GITHUB_REPO}"
  fi
else
  args+=(--issue "$GITHUB_ISSUE_NUMBER")
  echo "Syncing ${GITHUB_REPO}#${GITHUB_ISSUE_NUMBER} → Asana"
fi
if [ "$COLUMNS_ONLY" = "true" ]; then
  args+=(--columns-only)
  echo "Mode: columns-only (Status → Asana section)"
fi
uv run G2A "${args[@]}"
