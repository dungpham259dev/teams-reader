"""List chats currently in the store (name + message count), for verification."""
from teams_reader import config
from teams_reader.store import db

conn = db.connect(config.db_path())
rows = db.list_chats(conn)
named = sum(1 for r in rows if r["display_name"])
print(f"chats: {len(rows)}  named: {named}")
for r in sorted(rows, key=lambda x: -(x["message_count"] or 0)):
    print(f"  {r['message_count'] or 0:>4}  {r['kind'] or '?':<6}  {r['display_name'] or '(no name)'}")
