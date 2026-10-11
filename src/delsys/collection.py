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
        return emgOutputPath.with_name(emgOutputPath.stem.replace(marker, "_Info_", 1) + ".txt")
    return emgOutputPath.with_name(f"{emgOutputPath.stem}_Info.txt")

class CollectionInfoRecorder:
    """Write collection and subject metadata shared by EMG and resistance files."""

    def __init__(self,outputPath: Path,subject: SubjectInfo,emgPath: Path,trainerPath: Path | None,) -> None:
        self.outputPath = outputPath
        self.subject = subject
        self.emgPath = emgPath
        self.trainerPath = trainerPath

    def start(self) -> None:
        self.outputPath.parent.mkdir(parents=True, exist_ok=True)
        with self.outputPath.open("x", encoding="utf-8") as output:
            output.write("EMG Cycling collection information\n")
            output.write(f"Created: {datetime.now().astimezone().isoformat(timespec='seconds')}\n\n")
            output.write("[Subject]\n")
            output.write(f"Patient ID: {self.subject.patientId or '未填寫'}\n")
            output.write(f"Name: {self.subject.name or '未填寫'}\n")
            output.write(f"Gender: {self.subject.gender or '未指定'}\n\n")
            output.write("[Output files]\n")
            output.write(f"EMG: {self.emgPath.name}\n")
            if self.trainerPath:
                output.write(f"Resistance: {self.trainerPath.name}\n")

    def append(self, heading: str, lines: Sequence[str]) -> None:
        with self.outputPath.open("a", encoding="utf-8") as output:
            output.write(f"\n[{heading}]\n")
            output.write("\n".join(lines) + "\n")