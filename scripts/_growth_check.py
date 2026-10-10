import sqlite3, sys
sys.path.insert(0, "src")
conn = sqlite3.connect("data/pipi.db")
cur = conn.cursor()
cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND (name LIKE '%growth%' OR name LIKE '%seed%' OR name LIKE '%milestone%')")
tables = [t[0] for t in cur.fetchall()]
print("表:", tables)
for t in tables:
    cur.execute(f"SELECT * FROM {t} LIMIT 8")
    rows = cur.fetchall()
    cols = [d[0] for d in cur.description]
    print(f"-- {t} ({len(rows)} 行): {cols}")
    for r in rows:
        # 缩短长字段
        print("   ", tuple((str(x)[:60] if isinstance(x, (str, bytes)) else x) for x in r))
conn.close()
