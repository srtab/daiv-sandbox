CMD_INIT_META_SCRIPT = """\
set -euo pipefail

META=/workdir/meta
NEW=/workdir/new
EXCLUDES="$META/.git-excludes"

rm -rf "$META"
mkdir -p "$META"

cat > "$EXCLUDES" <<'EOF'
.git
.git/
EOF

git -C "$META" init -q
git -C "$META" config user.name daiv-sandbox
git -C "$META" config user.email daiv-sandbox@local
git -C "$META" config core.excludesFile "$EXCLUDES"

# Empty root commit so HEAD~1 always exists from the very first turn.
git -C "$META" commit -q --allow-empty -m "root"

# The seeded state becomes the second commit. HEAD = seed, HEAD~1 = root.
git -C "$META" --work-tree="$NEW" add -A
git -C "$META" --work-tree="$NEW" commit -q --allow-empty -m "seed"
"""


CMD_TURN_DIFF_SCRIPT = """\
set -euo pipefail

META=/workdir/meta
NEW=/workdir/new

git -C "$META" --work-tree="$NEW" add -A
git -C "$META" --work-tree="$NEW" commit -q --allow-empty -m "turn"
git -C "$META" -c diff.renames=true diff -M --binary HEAD~1..HEAD
"""
