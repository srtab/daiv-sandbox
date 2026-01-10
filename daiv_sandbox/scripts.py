CMD_GIT_DIFF_EXTRACTOR_SCRIPT = """\
set -euo pipefail

OLD="/workdir/old/."
NEW="/workdir/new/."
META="/workdir/meta"
EXCLUDES="$META/.git-excludes"

# Clean up old directories and metadata (but not NEW, which is read-only mounted).
rm -rf "$META" "$OLD/.git"
mkdir -p "$META"

# Create excludes file to ignore .git directories without modifying the source trees
cat > "$EXCLUDES" << 'EOF'
.git
.git/
EOF

# Capture OLD and NEW as two commits in a tiny temp repo
git -C "$META" init -q
git -C "$META" config user.name daiv-sandbox
git -C "$META" config user.email daiv-sandbox@local
git -C "$META" config core.excludesFile "$EXCLUDES"

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
