#!/bin/sh
# Run before every push: fails if a tracked file looks personal or secret, or is over 1 MB.
# sh scripts/public-check.sh   (PUBLIC_CHECK_EXTRA="word1|word2" blocks more words, given at run time only)
cd "$(git rev-parse --show-toplevel)" || exit 1
pattern='/Users/|/home/[a-z]|C:\\Users|@gmail\.|@[a-z0-9-]+\.com\.tr|BEGIN [A-Z ]*PRIVATE KEY|sk-ant-|sk-[A-Za-z0-9]{20}|ghp_[A-Za-z0-9]{20}|AKIA[0-9A-Z]{16}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
[ -n "$PUBLIC_CHECK_EXTRA" ] && pattern="$pattern|$PUBLIC_CHECK_EXTRA"
hits=$(git ls-files -z | xargs -0 grep -nIE "$pattern" -- 2>/dev/null | grep -v '^scripts/public-check.sh:')
big=$(git ls-files -z | xargs -0 du -k 2>/dev/null | awk '$1 > 1024 { print $2 " (" $1 " KB)" }')
[ -z "$hits$big" ] && { echo "public check: clean"; exit 0; }
[ -n "$hits" ] && { echo "public check: personal or secret-looking lines:"; echo "$hits"; }
[ -n "$big" ] && { echo "public check: files over 1 MB:"; echo "$big"; }
exit 1
