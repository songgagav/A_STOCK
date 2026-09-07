import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from arctic_store import get_store

s = get_store()
df = s.read_trades()
print("total rows:", 0 if df is None else len(df))
if df is not None and len(df):
    print("cols:", list(df.columns))
    show = ["ts", "day", "symbol", "direction", "qty", "price", "fee", "pnl", "source"]
    print(df[[c for c in show if c in df.columns]].sort_index().to_string())
    print("\nby day:", df.groupby("day").size().to_dict())
    print("fee notna:", int(df["fee"].notna().sum()) if "fee" in df else "no fee col")
    print("pnl notna:", int(df["pnl"].notna().sum()) if "pnl" in df else "no pnl col")
