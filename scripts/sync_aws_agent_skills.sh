#!/usr/bin/env bash
# scripts/sync_aws_agent_skills.sh
#
# Keeps .kiro/skills/aws-agents/ mirrored from the upstream AWS
# "agent-toolkit-for-aws" repo's aws-agents plugin skills directory
# (https://github.com/aws/agent-toolkit-for-aws/tree/main/plugins/aws-agents/skills),
# so this workspace never works from a stale copy of AWS-maintained
# skills content.
#
# How it works:
#   1. A single `git ls-remote` call gets the latest commit SHA for the
#      tracked branch -- no clone is needed just to check for
#      staleness.
#   2. If that SHA matches the SHA recorded from the last sync (stored
#      in .kiro/skills/aws-agents/.sync-manifest.json), nothing else
#      happens: already up to date, exit 0.
#   3. If it differs (or no manifest exists yet), a cached
#      partial/sparse clone of the upstream repo (kept at
#      .kiro/.cache/aws-agent-toolkit-for-aws, gitignored) is created
#      or fast-forwarded, and only the target skills subdirectory is
#      mirrored -- via `rsync --delete`, so upstream deletions/renames
#      are reflected too, not just additions -- into
#      .kiro/skills/aws-agents/. The new commit SHA is then recorded in
#      the manifest.
#
# Usage:
#   scripts/sync_aws_agent_skills.sh            # check, and sync if stale (default). Always exits 0 -- safe to run from a hook.
#   scripts/sync_aws_agent_skills.sh --check    # report staleness only, never touches disk. Exits 1 if stale, 0 if up to date.
#
# Configuration (all optional env var overrides):
#   SKILLS_SOURCE_REPO     default: https://github.com/aws/agent-toolkit-for-aws.git
#   SKILLS_SOURCE_REF      default: main
#   SKILLS_SOURCE_SUBPATH  default: plugins/aws-agents/skills
#   SKILLS_TARGET_DIR      default: <workspace root>/.kiro/skills/aws-agents
#   SKILLS_CACHE_DIR       default: <workspace root>/.kiro/.cache/aws-agent-toolkit-for-aws
#
# This script runs automatically at the start of every Kiro session via
# the "sync-aws-agent-skills" SessionStart hook
# (.kiro/hooks/sync-aws-agent-skills.json), so skills are refreshed
# before any work begins in that session. A hook exit code other than 0
# or 2 is treated by Kiro as a silent, non-blocking failure, so network
# errors here are deliberately swallowed (logged, then `exit 0`) rather
# than surfaced as a hook failure -- a session should never fail to
# start just because GitHub was briefly unreachable.

set -uo pipefail

SOURCE_REPO="${SKILLS_SOURCE_REPO:-https://github.com/aws/agent-toolkit-for-aws.git}"
SOURCE_REF="${SKILLS_SOURCE_REF:-main}"
SOURCE_SUBPATH="${SKILLS_SOURCE_SUBPATH:-plugins/aws-agents/skills}"

# Resolve paths relative to the workspace root (this script's parent's
# parent directory: scripts/..), regardless of the caller's cwd.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

TARGET_DIR="${SKILLS_TARGET_DIR:-${WORKSPACE_ROOT}/.kiro/skills/aws-agents}"
CACHE_DIR="${SKILLS_CACHE_DIR:-${WORKSPACE_ROOT}/.kiro/.cache/aws-agent-toolkit-for-aws}"
MANIFEST_FILE="${TARGET_DIR}/.sync-manifest.json"

CHECK_ONLY=0
if [[ "${1:-}" == "--check" ]]; then
  CHECK_ONLY=1
fi

log() { printf '[sync-aws-agent-skills] %s\n' "$1"; }

# Read back the commit SHA recorded by the previous sync, if any. Parsed
# with grep/sed rather than a JSON library since the manifest is a
# small, fixed format we control ourselves.
local_sha=""
if [[ -f "${MANIFEST_FILE}" ]]; then
  local_sha="$(grep -o '"synced_commit"[[:space:]]*:[[:space:]]*"[^"]*"' "${MANIFEST_FILE}" \
    | sed -E 's/.*:[[:space:]]*"([^"]*)"/\1/' || true)"
fi

remote_sha="$(git ls-remote "${SOURCE_REPO}" "refs/heads/${SOURCE_REF}" 2>/dev/null | cut -f1)"

if [[ -z "${remote_sha}" ]]; then
  log "could not reach ${SOURCE_REPO} (offline or GitHub unreachable) -- skipping freshness check."
  exit 0
fi

if [[ "${remote_sha}" == "${local_sha}" ]]; then
  log "up to date (commit ${remote_sha:0:12})."
  exit 0
fi

if [[ "${CHECK_ONLY}" -eq 1 ]]; then
  if [[ -z "${local_sha}" ]]; then
    log "STALE: skills have never been synced locally. Remote is at ${remote_sha:0:12}."
  else
    log "STALE: local copy is at ${local_sha:0:12}, remote is at ${remote_sha:0:12}."
  fi
  exit 1
fi

log "new commit detected (local: ${local_sha:-none}, remote: ${remote_sha:0:12}) -- syncing..."

mkdir -p "$(dirname "${CACHE_DIR}")"

if [[ -d "${CACHE_DIR}/.git" ]]; then
  if ! git -C "${CACHE_DIR}" fetch --depth 1 origin "${SOURCE_REF}" >/dev/null 2>&1; then
    log "fetch failed -- skipping sync this run (will retry next session)."
    exit 0
  fi
  git -C "${CACHE_DIR}" checkout -q FETCH_HEAD
else
  rm -rf "${CACHE_DIR}"
  if ! git clone -q --filter=blob:none --sparse --depth 1 --branch "${SOURCE_REF}" \
      "${SOURCE_REPO}" "${CACHE_DIR}" >/dev/null 2>&1; then
    log "clone failed -- skipping sync this run (will retry next session)."
    exit 0
  fi
  git -C "${CACHE_DIR}" sparse-checkout set "${SOURCE_SUBPATH}" >/dev/null 2>&1
fi

SOURCE_DIR="${CACHE_DIR}/${SOURCE_SUBPATH}"
if [[ ! -d "${SOURCE_DIR}" ]]; then
  log "expected subpath '${SOURCE_SUBPATH}' not found in ${SOURCE_REPO}@${SOURCE_REF} -- aborting sync."
  exit 0
fi

mkdir -p "${TARGET_DIR}"
# --delete mirrors upstream deletions/renames too, not just additions.
# The manifest lives inside TARGET_DIR but not in SOURCE_DIR, so it is
# excluded to survive the mirror.
rsync -a --delete --exclude ".sync-manifest.json" "${SOURCE_DIR}/" "${TARGET_DIR}/"

resolved_sha="$(git -C "${CACHE_DIR}" rev-parse HEAD)"
synced_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

cat > "${MANIFEST_FILE}" <<EOF
{
  "source_repo": "${SOURCE_REPO}",
  "source_ref": "${SOURCE_REF}",
  "source_subpath": "${SOURCE_SUBPATH}",
  "synced_commit": "${resolved_sha}",
  "synced_at": "${synced_at}"
}
EOF

skill_count="$(find "${TARGET_DIR}" -maxdepth 1 -mindepth 1 -type d | wc -l | tr -d ' ')"
log "synced to commit ${resolved_sha:0:12} (${skill_count} skill directories)."
exit 0
