from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_cli_run_dry_run_with_json_config(tmp_path: Path) -> None:
    cfg = {
        "symbols": ["AAPL", "MSFT"],
        "csv_path": "data/sample_prices.csv",
        "run_name": "cfg-run",
        "out_dir": str(tmp_path / "runs"),
        "database_url": f"sqlite:///{tmp_path / 'runs.db'}",
        "short_window": 5,
        "long_window": 10,
        "execution": {"rng_seed": 123},
    }
    p = tmp_path / "config.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, "-m", "quant_backtester.cli", "run", "--config", str(p), "--dry-run"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    assert "Config valid." in proc.stdout
    payload = json.loads("\n".join(proc.stdout.splitlines()[1:]))
    assert payload["run_name"] == "cfg-run"
    assert payload["symbols"] == ["AAPL", "MSFT"]


def test_cli_run_no_persist_does_not_create_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "runs.db"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "quant_backtester.cli",
            "run",
            "--csv",
            "data/sample_prices.csv",
            "--symbols",
            "AAPL,MSFT",
            "--short",
            "5",
            "--long",
            "10",
            "--db",
            f"sqlite:///{db_path}",
            "--out",
            str(tmp_path / "runs"),
            "--no-persist",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    assert not db_path.exists()


def test_cli_run_dry_run_reports_persist_false(tmp_path: Path) -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "quant_backtester.cli",
            "run",
            "--csv",
            "data/sample_prices.csv",
            "--symbols",
            "AAPL,MSFT",
            "--short",
            "5",
            "--long",
            "10",
            "--db",
            f"sqlite:///{tmp_path / 'runs.db'}",
            "--out",
            str(tmp_path / "runs"),
            "--no-persist",
            "--dry-run",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    payload = json.loads("\n".join(proc.stdout.splitlines()[1:]))
    assert payload["persist"] is False
