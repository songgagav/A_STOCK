# Project-pinned StockDB interface

This directory pins the StockDB Python interface used by the project:

- `pybao/stock_sdk.py`
- `pybao/stockdb.pyd`
- `pybao/zb_core.pyd`
- `pybao/zhibiao.py`
- `bin/stockdb.exe`

The market database itself is deliberately not stored in Git. `stockdb.exe`
must run with the deployment data root (normally `E:\A_stockDB`) as its working
directory so that its `data`, `data1`, `mydb`, and `stockdb.conf` remain outside
the repository. The project loader prefers this pinned interface and falls back
to an explicitly configured external installation only when the bundle is
absent.

The canonical runtime is Python 3.10 (`.venv310`). The native extension is
loaded only through `src/stockdb_runtime.py`; application code must not guess
`rd` query shapes or add another database.

## Contract

- Local endpoint: `127.0.0.1:7899`.
- Daily/minute bars require the full `table + code/prefix + date_query` key.
- Use `*` for matching, server-side projection/slicing, and `rd.pipe()` for
  discrete bulk operations.
- Private persistence stays in StockDB `rd` under `./mydb`; this repository's
  h5i analysis store remains a separate, explicit analysis layer.
- Do not commit deployment data, logs, credentials, or the vendor database
  directories.

The exact artifact hashes are recorded in `manifest.json`. Updating the vendor
build requires replacing the pinned files and updating that manifest in the
same commit.
