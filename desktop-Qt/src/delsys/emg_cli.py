from __future__ import annotations
import argparse
import csv
import sys
from pathlib import Path
from delsys.emg.sensor_mapping import csvColumnNames, loadProjectMappings
from delsys.emg.trigno import CHANNEL_COUNT, TrignoClient, TrignoError

def configure_console_encoding() -> None:
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
        with TrignoClient(host=args.host) as client, args.output.open("x", newline="", encoding="utf-8-sig") as output:
            print(f"Server: {client.connect()}")
            active = client.active_sensors(args.channels)
            if not active:
                raise TrignoError("找不到啟用中的 EMG 感測器，請先在 Trigno Control Utility 啟用感測器。")
            print(f"啟用 slot: {active or '無'}")
            activeIndices = [slot - 1 for slot in active]
            writer = csv.writer(output)
            sampleRate, _, _ = client.emg_frame_config()
            mappings = loadProjectMappings()
            activeMappings = [mappings[index] for index in activeIndices]
            writer.writerow(["time_seconds", *csvColumnNames(activeMappings)])
            client.start()
            target, count = round(args.duration * sampleRate), 0
            while count < target:
                for frame in client.read_emg_frames(args.channels):
                    if count >= target:
                        break
                    writer.writerow([count / sampleRate, *(frame[index] for index in activeIndices)])
                    count += 1
            client.stop()
        print(f"已輸出 {count} samples（SDK EMG 頻率 {sampleRate:.3f} Hz）至 {args.output.resolve()}")
        return 0
    except (FileExistsError, OSError, TrignoError) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
