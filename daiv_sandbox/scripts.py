CMD_GIT_DIFF_EXTRACTOR_SCRIPT = """\
set -euo pipefail

OLD="/workdir/old/{repo_workdir}"
NEW="/workdir/new/{repo_workdir}"
META="/workdir/meta"

# Clean up old directories and create new ones.
rm -rf "$META" "$OLD/.git" "$NEW/.git"
mkdir -p "$META"

# Capture OLD and NEW as two commits in a tiny temp repo
git -C "$META" init -q
git -C "$META" config user.name daiv-sandbox
git -C "$META" config user.email daiv-sandbox@local

# commit baseline (OLD)
git -C "$META" --work-tree="$OLD" add -A
git -C "$META" --work-tree="$OLD" commit -qm "baseline"

BASE_COMMIT=$(git -C "$META" rev-parse HEAD)

# commit post-sandbox (NEW)
git -C "$META" --work-tree="$NEW" add -A
git -C "$META" --work-tree="$NEW" commit -qm "post"

# Emit only this turn's delta (binary-safe, rename-aware)
git -C "$META" -c diff.renames=true diff -M --binary "$BASE_COMMIT"..HEAD
"""
