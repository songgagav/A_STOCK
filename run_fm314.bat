@echo off
REM py3.14 因子挖掘工具集入口 — ai-factor-lab / QuantGplearn / FactorMiner
REM 用法: run_fm314 quick-check|gp-mine|gp-smoke|gp-list|registry|miner-register|miner-report
"%~dp0.venv314\Scripts\python.exe" "%~dp0src\run_fm314.py" %*