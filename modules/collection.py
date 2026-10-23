from __future__ import annotations
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

@dataclass(frozen=True)
class SubjectInfo:
    patientId: str = ""
    name: str = ""
    gender: str = "未指定"

def infoOutputPath(emgOutputPath: Path) -> Path:
    marker = "_EMG_"
    if marker in emgOutputPath.stem:
        name = emgOutputPath.stem.replace(marker, "_Info_", 1)
        return emgOutputPath.with_name(name + ".txt")
    return emgOutputPath.with_name(f"{emgOutputPath.stem}_Info.txt")


def analysisOutputPath(emgOutputPath: Path) -> Path:
    """Return the paired analysis JSON path using the shared collection date."""
    marker = "_EMG_"
    if marker in emgOutputPath.stem:
        name = emgOutputPath.stem.replace(marker, "_Analysis_", 1)
        return emgOutputPath.with_name(name + ".json")
    return emgOutputPath.with_name(f"{emgOutputPath.stem}_Analysis.json")

class CollectionInfoRecorder:
    """Write one consistently ordered UTF-8 collection-information file."""

    sectionOrder = ("EMG", "阻力計", "踏板")

    def __init__(self,outputPath: Path,subject: SubjectInfo,emgPath: Path,trainerPath: Path | None,) -> None:
        self.outputPath = outputPath
        self.subject = subject
        self.emgPath = emgPath
        self.trainerPath = trainerPath
        self.testTime = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
        self._settings: dict[str, list[str]] = {}
        self._data: dict[str, list[str]] = {}

    def start(self) -> None:
        self.outputPath.parent.mkdir(parents=True, exist_ok=True)
        self._write("x")

    def setSettings(self, section: str, lines: Sequence[str]) -> None:
        """Save the settings present when one device started recording."""
        self._settings[section] = list(lines)
        self._write("w")

    def append(self, heading: str, lines: Sequence[str]) -> None:
        """Store collected values and rewrite the file in its canonical order."""
        section, kind = {
            "EMG 資料": ("EMG", "settings"),
            "EMG 測試摘要": ("EMG", "data"),
            "阻力計資料": ("阻力計", "data"),
            "踏板資料": ("踏板", "data"),
        }.get(heading, (heading, "data"))
        if kind == "settings":
            self._settings[section] = list(lines)
        else:
            self._data[section] = list(lines)
        self._write("w")

    def _write(self, mode: str) -> None:
        with self.outputPath.open(mode, encoding="utf-8") as output:
            output.write("EMG Cycling 測試資訊\n")
            output.write("=" * 32 + "\n\n")
            output.write("[病人資訊]\n")
            output.write(f"病人 ID：{self.subject.patientId or '未填寫'}\n")
            output.write(f"測試者：{self.subject.name or '未填寫'}\n")
            output.write(f"性別：{self.subject.gender or '未指定'}\n\n")
            settings = [line for section in self.sectionOrder for line in self._settings.get(section, [])]
            data = {section: self._data[section] for section in self.sectionOrder if section in self._data}
            if settings:
                output.write("\n【參數設定】\n")
                output.write("\n".join(settings) + "\n")
            if data:
                output.write("\n【測試數據】\n")
                if "EMG" in data:
                    output.write("EMG\n")
                    output.write("\n".join(data["EMG"]) + "\n")
                cycling = [line for section in ("阻力計", "踏板") for line in data.get(section, [])]
                if cycling:
                    output.write("\n阻力與踏板\n")
                    output.write("\n".join(cycling) + "\n")
