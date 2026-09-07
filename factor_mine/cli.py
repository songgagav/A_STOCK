# -*- coding: utf-8 -*-
"""评估 CLI: 从 parquet/CSV 面板对候选因子跑统一评估并写入台账.
用法: python -m factor_mine.cli --parquet <file> --factor ret_20 --fwd fwd_5 \
      --start 2020-01-01 --end 2020-12-31 [--register]
"""
import argparse
import duckdb
import pandas as pd

from factor_mine.evaluator import evaluate
from factor_mine import ledger


def load(parquet: str, start: str, end: str,
         cols: list[str]) -> pd.DataFrame:
    con = duckdb.connect()
    keep = ", ".join(f'"{c}"' for c in cols)
    sql = (f"SELECT {keep} FROM '{parquet}' "
           f"WHERE date BETWEEN '{start}' AND '{end}'")
    df = con.execute(sql).fetchdf()
    con.close()
    return df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--parquet", required=True)
    ap.add_argument("--factor", required=True)
    ap.add_argument("--fwd", default="fwd_5")
    ap.add_argument("--start", default="2020-01-01")
    ap.add_argument("--end", default="2020-12-31")
    ap.add_argument("--register", action="store_true", help="登记候选并写入评估报告")
    args = ap.parse_args()

    df = load(args.parquet, args.start, args.end,
              ["date", "symbol", args.factor, args.fwd])
    df = df.dropna(subset=[args.factor, args.fwd])
    rep = evaluate(df, args.factor, args.fwd)
    print(rep)
    if args.register:
        ledger.register(args.factor, expr=args.factor,
                        source="smoke_features_2015_2021", note="统一评估冒烟")
        ledger.append_report(args.factor, rep)


if __name__ == "__main__":
    main()
