# Command to create an ephemeral repo based on two directories and makes two throwaway commits.
# Git will then respect the repo's .gitignore automatically, and the diff won't include them.
CMD_EPHEMERAL_REPO = """set -euo pipefail
A=/a/{workdir}
B=/b/{workdir}
W=/tmp/work; rm -rf "$W"; mkdir -p "$W"; cd "$W"
git config --global --add safe.directory /tmp/work
git init -q
git config user.name sandbox
git config user.email sandbox@example.local
cp -a "$A"/. .
git add -A && git commit -q -m baseline
find . -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf -- {{}} +
cp -a "$B"/. .
git add -A && git commit -q --allow-empty -m after
"""

# Command to extract the diff between the two directories as a binary diff.
CMD_GIT_DIFF_BINARY = "git -c core.quotepath=false diff --binary HEAD~1 HEAD || true"
