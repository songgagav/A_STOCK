import os
import duckdb, json
con = duckdb.connect(os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "legacy_stockdb.duckdb"), read_only=True)

# Check the pool_snapshot for 2026-09-02
pool_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "daily", "20260902", "pool_snapshot.json")
with open(pool_path, encoding="utf-8") as f:
    pool = json.load(f)
print("pool keys:", list(pool.keys())[:10] if isinstance(pool, dict) else "not dict")
if isinstance(pool, dict):
    if "top_n" in pool:
        print("top_n count:", len(pool["top_n"]))
        print("top_n samples:", pool["top_n"][:3])
    if "pool_size" in pool:
        print("pool_size:", pool["pool_size"])
    if "selection" in pool:
        sel = pool["selection"]
        if isinstance(sel, dict) and "top_n" in sel:
            print("selection top_n:", [t.get("canon") for t in sel["top_n"][:5]])

# Check the selection.json
sel_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "daily", "20260902", "selection.json")
with open(sel_path, encoding="utf-8") as f:
    sel = json.load(f)
print("\nselection.json keys:", list(sel.keys())[:10])
if "top_n" in sel:
    print("top_n count:", len(sel["top_n"]))
    print("top_n canons:", [t.get("canon") for t in sel["top_n"]])
con.close()
