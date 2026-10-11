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

class CollectionInfoRecorder:
    """Write one UTF-8 Traditional-Chinese information file per collection."""

    def __init__(self,outputPath: Path,subject: SubjectInfo,emgPath: Path,trainerPath: Path | None,) -> None:
        self.outputPath = outputPath
        self.subject = subject
        self.emgPath = emgPath
        self.trainerPath = trainerPath

    def start(self) -> None:
        self.outputPath.parent.mkdir(parents=True, exist_ok=True)
        testTime = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
        with self.outputPath.open("x", encoding="utf-8") as output:
            output.write("EMG Cycling 測試資訊\n")
            output.write("=" * 32 + "\n\n")
            output.write("[病人資訊]\n")
            output.write(f"病人 ID：{self.subject.patientId or '未填寫'}\n")
            output.write(f"測試者：{self.subject.name or '未填寫'}\n")
            output.write(f"性別：{self.subject.gender or '未指定'}\n\n")
            output.write("[測試資訊]\n")
            output.write(f"測試時間：{testTime}\n")
            output.write(f"EMG 資料檔：{self.emgPath.name}\n")
            if self.trainerPath:
                output.write(f"阻力計資料檔：{self.trainerPath.name}\n")

    def append(self, heading: str, lines: Sequence[str]) -> None:
        with self.outputPath.open("a", encoding="utf-8") as output:
            output.write(f"\n[{heading}]\n")
            output.write("\n".join(lines) + "\n")
