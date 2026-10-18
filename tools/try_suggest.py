"""Exercise suggest_reply / chat_profile on real chats, printing stats only
(participant profiles + counts), not message content."""
import sys

from teams_reader import config
from teams_reader.mcp import tools
from teams_reader.store import db

conn = db.connect(config.db_path())
target = sys.argv[1] if len(sys.argv) > 1 else "Dassone"

sr = tools.suggest_reply(conn, target)
print(f"suggest_reply('{target}'): chat={sr.get('chat_name')!r} kind={sr.get('kind')} "
      f"recent={len(sr.get('recent', []))}")
for sender, prof in sr.get("participants", {}).items():
    print(f"  - {sender}: lang={prof['language']} avg_words={prof['avg_words']} "
          f"emoji={prof['emoji_rate']} q={prof['question_rate']} style={prof['style']!r}")
print("guidance present:", "guidance" in sr)
