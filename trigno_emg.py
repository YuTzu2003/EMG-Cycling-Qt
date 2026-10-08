#!/usr/bin/env python3
"""Read EMG samples from the Delsys Trigno SDK Server over TCP."""

from __future__ import annotations

import argparse
import csv
import math
import socket
import struct
import sys
import time
from pathlib import Path
from typing import Optional, TextIO


MESSAGE_END = b"\r\n\r\n"


def configure_console_encoding() -> None:
    """Keep Chinese status messages readable in Windows terminals and pipes."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")


class TrignoError(RuntimeError):
    """Raised when the SDK Server cannot complete a requested operation."""


class TrignoClient:
    def __init__(
        self,
        host: str = "127.0.0.1",
        command_port: int = 50040,
        emg_port: int = 50041,
        aux_port: int = 50042,
        timeout: float = 5.0,
    ) -> None:
        self.host = host
        self.command_port = command_port
        self.emg_port = emg_port
        self.aux_port = aux_port
        self.timeout = timeout
        self.command_socket: Optional[socket.socket] = None
        self.emg_socket: Optional[socket.socket] = None
        self.aux_socket: Optional[socket.socket] = None
        self._command_buffer = b""
        self._emg_buffer = bytearray()
        self.byte_order = "<"
        self.streaming = False

    def connect(self) -> str:
        """Connect all three sockets and return the server banner."""
        try:
            self.command_socket = socket.create_connection(
                (self.host, self.command_port), self.timeout
            )
            self.command_socket.settimeout(self.timeout)
            banner = self._read_command_message()

            # SDK Server 3.x expects both data clients before accepting START,
            # even when the caller only needs the EMG stream.
            self.emg_socket = socket.create_connection(
                (self.host, self.emg_port), self.timeout
            )
            self.emg_socket.settimeout(1.0)
            self.aux_socket = socket.create_connection(
                (self.host, self.aux_port), self.timeout
            )
            self.aux_socket.settimeout(0.2)
            self._emg_buffer.clear()
            endianness = self.command("ENDIANNESS?").upper()
            if "LITTLE" in endianness:
                self.byte_order = "<"
            elif "BIG" in endianness:
                self.byte_order = ">"
            else:
                raise TrignoError(f"無法辨識 Server 資料位元序：{endianness}")
            return banner
        except (OSError, socket.timeout) as exc:
            self.close()
            raise TrignoError(f"無法連線到 Trigno SDK Server：{exc}") from exc

    def _read_command_message(self) -> str:
        if self.command_socket is None:
            raise TrignoError("Command socket 尚未連線")

        while MESSAGE_END not in self._command_buffer:
            chunk = self.command_socket.recv(4096)
            if not chunk:
                raise TrignoError("Command socket 已被 Server 關閉")
            self._command_buffer += chunk

        message, self._command_buffer = self._command_buffer.split(MESSAGE_END, 1)
        return message.decode("ascii", errors="replace").strip()

    def command(self, text: str) -> str:
        if self.command_socket is None:
            raise TrignoError("Command socket 尚未連線")
        self.command_socket.sendall(text.encode("ascii") + MESSAGE_END)
        return self._read_command_message()

    def sensor_states(self, sensor_count: int = 16) -> tuple[list[int], list[int]]:
        paired = []
        active = []
        for sensor_number in range(1, sensor_count + 1):
            response = self.command(f"SENSOR {sensor_number} PAIRED?")
            if response.upper() == "YES":
                paired.append(sensor_number)
                if self.command(f"SENSOR {sensor_number} ACTIVE?").upper() == "YES":
                    active.append(sensor_number)
        return paired, active

    def start(self) -> None:
        response = self.command("START")
        if response.upper() != "OK":
            raise TrignoError(f"START 被 Server 拒絕：{response}")
        self.streaming = True

    def stop(self) -> None:
        if self.command_socket is None:
            return
        response = self.command("STOP")
        self.streaming = False
        if response.upper() != "OK":
            raise TrignoError(f"STOP 被 Server 拒絕：{response}")

    def read_emg_frames(self, channel_count: int = 16) -> list[tuple[float, ...]]:
        """Read currently available multiplexed float32 EMG frames.

        A socket read is not guaranteed to end on a frame boundary, so incomplete
        bytes are retained for the next call. A timeout simply returns no frames.
        """
        if self.emg_socket is None:
            raise TrignoError("EMG socket 尚未連線")
        if channel_count <= 0:
            raise ValueError("channel_count 必須大於 0")

        try:
            chunk = self.emg_socket.recv(65536)
        except socket.timeout:
            return []
        if not chunk:
            raise TrignoError("EMG socket 已被 Server 關閉")

        self._emg_buffer.extend(chunk)
        frame_size = channel_count * 4
        frame_count = len(self._emg_buffer) // frame_size
        if frame_count == 0:
            return []

        unpack_frame = struct.Struct(f"{self.byte_order}{channel_count}f").unpack
        consumed = frame_count * frame_size
        frames = [
            unpack_frame(self._emg_buffer[offset : offset + frame_size])
            for offset in range(0, consumed, frame_size)
        ]
        del self._emg_buffer[:consumed]
        return frames

    def capture(
        self,
        duration: float,
        sample_rate: float,
        channel_count: int,
        output: TextIO,
        show_live: bool = True,
    ) -> tuple[int, list[float], list[float], list[float]]:
        """Capture fixed-width, little-endian float32 frames into a CSV file."""
        if self.emg_socket is None:
            raise TrignoError("EMG socket 尚未連線")

        target_samples = max(1, round(duration * sample_rate))
        writer = csv.writer(output)
        writer.writerow(
            ["sample", "time_seconds"]
            + [f"sensor_{number}_emg" for number in range(1, channel_count + 1)]
        )

        minima = [math.inf] * channel_count
        maxima = [-math.inf] * channel_count
        sum_squares = [0.0] * channel_count
        sample_index = 0
        next_status = time.monotonic() + 0.5

        while sample_index < target_samples:
            frames = self.read_emg_frames(channel_count)
            frames_to_write = min(len(frames), target_samples - sample_index)
            for values in frames[:frames_to_write]:
                writer.writerow([sample_index, sample_index / sample_rate, *values])
                for channel, value in enumerate(values):
                    minima[channel] = min(minima[channel], value)
                    maxima[channel] = max(maxima[channel], value)
                    sum_squares[channel] += value * value
                sample_index += 1
            now = time.monotonic()
            if show_live and now >= next_status:
                print(
                    f"\r已接收 {sample_index:>7}/{target_samples} 筆 "
                    f"({sample_index / sample_rate:5.2f} 秒)",
                    end="",
                    flush=True,
                )
                next_status = now + 0.5

        if show_live:
            print()
        rms = [math.sqrt(total / sample_index) for total in sum_squares]
        return sample_index, minima, maxima, rms

    def close(self) -> None:
        for sock in (self.aux_socket, self.emg_socket, self.command_socket):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        self.aux_socket = self.emg_socket = self.command_socket = None

    def __enter__(self) -> "TrignoClient":
        return self

    def __exit__(self, *_: object) -> None:
        if self.streaming:
            try:
                self.stop()
            except (OSError, TrignoError):
                pass
        self.close()


def positive_number(value: str) -> float:
    number = float(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("必須大於 0")
    return number


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="透過 TCP 從 Trigno SDK Server 擷取 EMG，並存成 CSV。"
    )
    parser.add_argument("--host", default="127.0.0.1", help="SDK Server 位址")
    parser.add_argument("--duration", type=positive_number, default=5.0, help="錄製秒數")
    parser.add_argument("--sample-rate", type=positive_number, default=2000.0)
    parser.add_argument("--channels", type=int, default=16, help="EMG 串流通道數")
    parser.add_argument("--output", type=Path, default=Path("emg_data.csv"))
    parser.add_argument("--quiet", action="store_true", help="不顯示擷取進度")
    return parser.parse_args()


def main() -> int:
    configure_console_encoding()
    args = parse_args()
    if args.channels <= 0:
        print("錯誤：--channels 必須大於 0", file=sys.stderr)
        return 2

    client = TrignoClient(host=args.host)
    try:
        with client:
            banner = client.connect()
            print(f"Server：{banner}")
            paired, active = client.sensor_states(min(args.channels, 16))
            if paired:
                print("已配對感測器：" + ", ".join(map(str, paired)))
                if active:
                    print("已啟用感測器：" + ", ".join(map(str, active)))
                else:
                    print("警告：已配對的感測器均未啟用；資料可能全部為 0。")
            else:
                print("警告：Server 回報沒有已配對感測器；資料可能全部為 0。")

            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("w", newline="", encoding="utf-8") as csv_file:
                client.start()
                print(f"START: OK，開始擷取 {args.duration:g} 秒 EMG...")
                count, minima, maxima, rms = client.capture(
                    args.duration,
                    args.sample_rate,
                    args.channels,
                    csv_file,
                    not args.quiet,
                )
                client.stop()
                print("STOP: OK")

        print(f"完成：{count} 筆資料已寫入 {args.output.resolve()}")
        print("通道統計（min / max / RMS）：")
        for index, values in enumerate(zip(minima, maxima, rms), start=1):
            print(f"  {index:>2}: {values[0]: .7g} / {values[1]: .7g} / {values[2]: .7g}")
        if all(value == 0.0 for value in rms):
            print("警告：所有通道皆為 0，請在 Trigno SDK Server 中配對並啟用感測器。")
        return 0
    except (OSError, socket.timeout, TrignoError) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n使用者中止；正在停止串流...", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
