#!/usr/bin/env python3
"""
median_stats_builder.py

Standalone script -- does NOT touch the existing chronogenesis pipeline
in any way. Only reads the 998/999 median CSVs it already produces
(998-median_mid-pack_live_resorted.csv and 999-median_floor_live_resorted.csv,
via paths.csv_dir in the config), computes derived per-tier stats, and
writes/pushes a JSON file to a separately-cloned copy of the raw-data
GitHub repo (paths.output_repo_dir in the config).

CSV FORMAT (unchanged from the pipeline's own docs in
00-chronoleaderboard_report_prod.py): headerless paste-block, one column
per day, chronological order. Per column, top to bottom:
    row 0       time (HH:MM)
    row 1       month (MM)
    row 2       day (DD)
    row 3       blank
    rows 4-10   Total Club Fan, one row per tier (SS, S+, S, A+, A, B+, B)
    rows 11-17  Club Day-to-Day Delta, same tier order

METRICS (per tier, per day, computed independently within each
calendar-month group -- fan counts reset monthly, so "all-time" here
means "since day 1 of that tracked month"):
    club_day_to_day       the delta value straight from the CSV
    club_all_time_avg     running average of every non-blank delta from
                           day 1 of the month through the current day
    club_3day_avg         average of whichever of the current day and
                           the two days before it (within the same
                           month) have a non-blank delta -- a fixed
                           3-day window, not "reach back for 3 real
                           values"
    person_*               the three "club_*" values above, divided by
                           30 (assumed club size)
Blank/missing deltas are skipped entirely (never treated as 0), matching
how Google Sheets' AVERAGE() already behaves.

USAGE:
    python3 median_stats_builder.py [--config path/to/config.yaml]
"""

import argparse
import csv
import json
import logging
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

TIER_ORDER = ["ss", "s_plus", "s", "a_plus", "a", "b_plus", "b"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("median_stats_builder")


def load_config(config_path):
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _to_float_or_none(cell):
    cell = (cell or "").strip()
    if cell == "":
        return None
    try:
        return float(cell)
    except ValueError:
        return None


def parse_paste_block_csv(csv_path):
    """
    Parse one 998/999-style paste-block CSV into a list of column dicts:
    [{month, day, time, totals: {tier: float|None}, deltas: {tier: float|None}}, ...]
    Preserves the file's own column (chronological) order.
    """
    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f))

    if len(rows) < 18:
        raise ValueError(
            f"{csv_path} has {len(rows)} rows, expected at least 18 "
            "(3 header rows + 1 blank + 7 total rows + 7 delta rows)"
        )

    time_row, month_row, day_row = rows[0], rows[1], rows[2]
    total_rows = rows[4:11]
    delta_rows = rows[11:18]

    n_cols = len(time_row)
    columns = []
    for col_idx in range(n_cols):
        month_cell = (month_row[col_idx] if col_idx < len(month_row) else "").strip()
        day_cell = (day_row[col_idx] if col_idx < len(day_row) else "").strip()
        if month_cell == "" or day_cell == "":
            continue  # skip any trailing/empty column

        totals = {}
        deltas = {}
        for tier, trow in zip(TIER_ORDER, total_rows):
            totals[tier] = _to_float_or_none(trow[col_idx] if col_idx < len(trow) else "")
        for tier, drow in zip(TIER_ORDER, delta_rows):
            deltas[tier] = _to_float_or_none(drow[col_idx] if col_idx < len(drow) else "")

        columns.append({
            "month": int(month_cell),
            "day": int(day_cell),
            "time": (time_row[col_idx] if col_idx < len(time_row) else "").strip(),
            "totals": totals,
            "deltas": deltas,
        })

    return columns


def compute_stats(columns, club_size=30):
    """
    Groups columns by month (preserving file order within each group),
    then walks each month's columns in order computing the six metrics
    per tier. Returns the same columns, each with a new "stats" key:
    {tier: {club_day_to_day, club_all_time_avg, club_3day_avg,
             person_day_to_day, person_all_time_avg, person_3day_avg}}.
    """
    # Group column INDICES by month, preserving overall file order.
    month_groups = {}
    for idx, col in enumerate(columns):
        month_groups.setdefault(col["month"], []).append(idx)

    for tier in TIER_ORDER:
        for month, idxs in month_groups.items():
            running_deltas = []  # non-blank deltas seen so far this month, in order
            for pos, idx in enumerate(idxs):
                delta = columns[idx]["deltas"].get(tier)
                if delta is not None:
                    running_deltas.append(delta)

                club_all_time_avg = (
                    sum(running_deltas) / len(running_deltas) if running_deltas else None
                )

                # Fixed 3-day window: this day + up to 2 previous days
                # IN THIS MONTH, skipping any blank among them.
                window_idxs = idxs[max(0, pos - 2): pos + 1]
                window_deltas = [
                    columns[i]["deltas"].get(tier)
                    for i in window_idxs
                    if columns[i]["deltas"].get(tier) is not None
                ]
                club_3day_avg = (
                    sum(window_deltas) / len(window_deltas) if window_deltas else None
                )

                columns[idx].setdefault("stats", {})[tier] = {
                    "club_day_to_day": delta,
                    "club_all_time_avg": club_all_time_avg,
                    "club_3day_avg": club_3day_avg,
                    "person_day_to_day": delta / club_size if delta is not None else None,
                    "person_all_time_avg": (
                        club_all_time_avg / club_size if club_all_time_avg is not None else None
                    ),
                    "person_3day_avg": (
                        club_3day_avg / club_size if club_3day_avg is not None else None
                    ),
                }

    return columns


def filter_range(columns, start_month, start_date, end_month, end_date):
    """
    Slices columns (already in chronological file order) between the
    first column matching (start_month, start_date) and the last
    column matching (end_month, end_date), inclusive. Any bound left
    as None defaults to the earliest/latest available column -- this
    also covers the "all four blank" auto case.
    """
    if not columns:
        return columns

    start_idx = 0
    if start_month is not None and start_date is not None:
        for i, col in enumerate(columns):
            if col["month"] == start_month and col["day"] == start_date:
                start_idx = i
                break
        else:
            log.warning("start_month/start_date not found in CSV; using earliest available")

    end_idx = len(columns) - 1
    if end_month is not None and end_date is not None:
        for i in range(len(columns) - 1, -1, -1):
            col = columns[i]
            if col["month"] == end_month and col["day"] == end_date:
                end_idx = i
                break
        else:
            log.warning("end_month/end_date not found in CSV; using latest available")

    if start_idx > end_idx:
        log.warning("Resolved start is after end; swapping")
        start_idx, end_idx = end_idx, start_idx

    return columns[start_idx: end_idx + 1]


def build_dataset_json(columns):
    return {
        "days": [
            {
                "month": col["month"],
                "day": col["day"],
                "time": col["time"],
                "tiers": col["stats"],
            }
            for col in columns
        ]
    }


def process_source(csv_path, range_cfg):
    log.info("Reading %s", csv_path)
    columns = parse_paste_block_csv(csv_path)
    columns = compute_stats(columns)
    columns = filter_range(
        columns,
        range_cfg.get("start_month"),
        range_cfg.get("start_date"),
        range_cfg.get("end_month"),
        range_cfg.get("end_date"),
    )
    log.info("  -> %d day-columns after range filtering", len(columns))
    return build_dataset_json(columns)


def git_commit_and_push(repo_dir, commit_message, branch):
    def run(*args):
        result = subprocess.run(
            ["git", *args], cwd=repo_dir, capture_output=True, text=True
        )
        return result

    add = run("add", ".")
    if add.returncode != 0:
        log.error("git add failed: %s", add.stderr.strip())
        return False

    status = run("status", "--porcelain")
    if not status.stdout.strip():
        log.info("No changes to commit -- data is already up to date.")
        return True

    commit = run("commit", "-m", commit_message)
    if commit.returncode != 0:
        log.error("git commit failed: %s", commit.stderr.strip())
        return False

    push = run("push", "origin", branch)
    if push.returncode != 0:
        log.error("git push failed: %s", push.stderr.strip())
        return False

    log.info("Pushed to %s (%s)", branch, repo_dir)
    return True


def main():
    parser = argparse.ArgumentParser(description="Build median stats JSON and push to raw-data repo.")
    parser.add_argument(
        "--config",
        default=str(Path(__file__).with_name("median_stats_builder_config.yaml")),
        help="Path to config YAML (default: median_stats_builder_config.yaml next to this script)",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    paths = config["paths"]
    range_cfg = config.get("range") or {}
    git_cfg = config.get("git") or {}

    csv_dir = Path(paths["csv_dir"]).expanduser()
    mid_pack_path = csv_dir / paths["mid_pack_csv"]
    floor_path = csv_dir / paths["floor_csv"]

    if not mid_pack_path.exists():
        log.error("Mid-pack CSV not found: %s", mid_pack_path)
        sys.exit(1)
    if not floor_path.exists():
        log.error("Floor CSV not found: %s", floor_path)
        sys.exit(1)

    mid_pack_json = process_source(mid_pack_path, range_cfg)
    floor_json = process_source(floor_path, range_cfg)

    output = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tiers": TIER_ORDER,
        "mid_pack": mid_pack_json,
        "floor": floor_json,
    }

    output_repo_dir = Path(paths["output_repo_dir"]).expanduser()
    if not output_repo_dir.exists():
        log.error(
            "output_repo_dir does not exist: %s -- clone the raw-data repo there first.",
            output_repo_dir,
        )
        sys.exit(1)

    output_path = output_repo_dir / paths["output_json_name"]
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)
    log.info("Wrote %s", output_path)

    ok = git_commit_and_push(
        output_repo_dir,
        git_cfg.get("commit_message", "Update median stats"),
        git_cfg.get("branch", "main"),
    )
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
