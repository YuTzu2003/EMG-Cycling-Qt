#!/usr/bin/env python3
"""Command-line recorder for the Delsys Trigno SDK Server."""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from delsys.sensor_mapping import csvColumns, loadProjectMappings
from delsys.trigno import CHANNEL_COUNT, SAMPLE_RATE, TrignoClient, TrignoError


def configure_console_encoding() -> None:
    """Keep Traditional Chinese status text readable in Windows terminals."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8")


def positive_float(value: str) -> float:
    number = float(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("必須大於 0")
    return number


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="錄製 Trigno SDK Server 的 EMG 資料")
    parser.add_argument("--host", default="127.0.0.1", help="SDK Server IP 位址")
    parser.add_argument("--duration", type=positive_float, default=5.0, help="錄製秒數")
    parser.add_argument("--sample-rate", type=positive_float, default=SAMPLE_RATE)
    parser.add_argument("--channels", type=int, default=CHANNEL_COUNT)
    parser.add_argument("--output", type=Path, default=Path("emg_data.csv"))
    return parser.parse_args()


def main() -> int:
    configure_console_encoding()
    args = parse_args()
    if not 1 <= args.channels <= CHANNEL_COUNT:
        print(f"--channels 必須介於 1 到 {CHANNEL_COUNT}", file=sys.stderr)
        return 2
    try:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with TrignoClient(host=args.host) as client, args.output.open("x", newline="", encoding="utf-8") as output:
            print(f"Server: {client.connect()}")
            active = client.active_sensors(args.channels)
            print(f"啟用 slot: {active or '無'}")
            activeIndices = [slot - 1 for slot in active]
            writer = csv.writer(output)
            allColumns = csvColumns(loadProjectMappings())[:args.channels]
            writer.writerow(["sample", "time_seconds"] + [allColumns[index] for index in activeIndices])
            client.start()
            count = client.capture(args.duration, args.sample_rate, args.channels, writer, activeIndices)
            client.stop()
        print(f"已錄製 {count} 筆資料至 {args.output.resolve()}")
        return 0
    except (FileExistsError, OSError, TrignoError) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
