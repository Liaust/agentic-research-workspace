#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -ne 1 ]]; then
  echo "Usage: scripts/verify-public-release.sh <candidate-root>" >&2
  exit 2
fi

candidate_root="$1"

if [[ ! -d "$candidate_root" ]]; then
  echo "Candidate root does not exist: $candidate_root" >&2
  exit 2
fi

candidate_root="$(cd "$candidate_root" && pwd -P)"

verification_root="$candidate_root"
temporary_verification_root=""

cleanup() {
  if [[ -n "$temporary_verification_root" && -d "$temporary_verification_root" ]]; then
    rm -rf -- "$temporary_verification_root"
  fi
}
trap cleanup EXIT

candidate_git_root="$(git -C "$candidate_root" rev-parse --show-toplevel 2>/dev/null || true)"
if [[ -n "$candidate_git_root" ]]; then
  candidate_git_root="$(cd "$candidate_git_root" && pwd -P)"
fi
if [[ "$candidate_git_root" == "$candidate_root" ]]; then
  if [[ -n "$(git -C "$candidate_root" status --porcelain --untracked-files=all)" ]]; then
    echo "Candidate Git working tree has tracked or unignored changes" >&2
    exit 4
  fi
  temporary_verification_root="$(mktemp -d "${TMPDIR:-/tmp}/research-map-public-verify.XXXXXX")"
  git -C "$candidate_root" archive --format=tar HEAD |
    tar -xf - -C "$temporary_verification_root"
  verification_root="$temporary_verification_root"
fi

PYTHONDONTWRITEBYTECODE=1 uv run --isolated --frozen --project "$verification_root" \
  research-map verify-public-release \
  --candidate-root "$verification_root" \
  --json
