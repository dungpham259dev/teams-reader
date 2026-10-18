#!/usr/bin/env bash
# De-risk Cách 2 with real scroll-wheel events over the Teams message area.
# Open a long chat in Teams first. Ingest -> scroll up -> ingest -> show delta.
set -uo pipefail
TR="$HOME/Code/teams-reader/venv/bin/teams-reader"
DIR="$HOME/Code/teams-reader/tools"

total() { "$TR" ingest 2>/dev/null | grep -oE '[0-9]+ total' | grep -oE '^[0-9]+'; }

osascript -e 'tell application "Microsoft Teams" to activate' 2>/dev/null
sleep 1

# Front window position/size -> a point inside the message list (right-centre).
read -r px py sw sh < <(osascript -e '
tell application "System Events" to tell process "Microsoft Teams"
  set p to position of front window
  set s to size of front window
  return (item 1 of p) & " " & (item 2 of p) & " " & (item 1 of s) & " " & (item 2 of s)
end tell' 2>/dev/null | tr ',' ' ')
X=$(python3 -c "print(int($px + $sw*0.6))")
Y=$(python3 -c "print(int($py + $sh*0.45))")
echo "window: pos=($px,$py) size=($sw,$sh) -> scroll point ($X,$Y)"

before=$(total); echo "before: $before"
for round in 1 2 3; do
  "$HOME/Code/teams-reader/venv/bin/python" "$DIR/scroll_teams.py" "$X" "$Y" 20
  sleep 3
  now=$(total)
  echo "after round $round: $now (delta $(( now - before )))"
  before=$now
done
