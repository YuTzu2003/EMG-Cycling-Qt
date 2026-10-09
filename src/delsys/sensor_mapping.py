from __future__ import annotations
import json
import re
from dataclasses import dataclass
from pathlib import Path
from .trigno import CHANNEL_COUNT

@dataclass(frozen=True)
class SensorMapping:
    slot: int
    chinese_name: str
    english_name: str
    abbreviation: str

    def displayName(self) -> str:
        suffix = f" ({self.abbreviation})" if self.abbreviation else ""
        return f"{self.chinese_name} · {self.english_name}{suffix}"

    def csvColumnName(self) -> str:
        """Return a stable, analysis-friendly English CSV header for this slot."""
        english = re.sub(r"[^a-z0-9]+", "_", self.english_name.lower()).strip("_")
        side = self.abbreviation.rsplit("-", 1)[-1].lower() if "-" in self.abbreviation else ""
        if english == "unassigned" or not english:
            return f"unassigned_{self.slot:02d}"
        return f"{side}_{english}" if side in {"r", "l"} else english


def defaultMappings() -> list[SensorMapping]:
    return [SensorMapping(slot, "未指定", "Unassigned", "") for slot in range(1, CHANNEL_COUNT + 1)]


def loadMappings(path: Path) -> list[SensorMapping]:
    """Read the user-editable mapping file and validate all 16 SDK slots."""
    if not path.is_file():
        return defaultMappings()
    try:
        records = json.loads(path.read_text(encoding="utf-8"))
        mappings = [
            SensorMapping(
                slot=int(record["slot"]),
                chinese_name=str(record["chinese_name"]),
                english_name=str(record["english_name"]),
                abbreviation=str(record.get("abbreviation", "")),
            )
            for record in records
        ]
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return defaultMappings()
    by_slot = {mapping.slot: mapping for mapping in mappings}
    if set(by_slot) != set(range(1, CHANNEL_COUNT + 1)):
        return defaultMappings()
    return [by_slot[slot] for slot in range(1, CHANNEL_COUNT + 1)]


def saveMappings(path: Path, mappings: list[SensorMapping]) -> None:
    """Persist mappings edited in the GUI as readable UTF-8 JSON."""
    records = [
        {
            "slot": mapping.slot,
            "chinese_name": mapping.chinese_name,
            "english_name": mapping.english_name,
            "abbreviation": mapping.abbreviation,
        }
        for mapping in mappings
    ]
    path.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def projectMappingPath() -> Path:
    return Path(__file__).resolve().parents[2] / "sensor_mapping.json"

def loadProjectMappings() -> list[SensorMapping]:
    return loadMappings(projectMappingPath())

def csvColumns(mappings: list[SensorMapping]) -> list[str]:
    return [mapping.csvColumnName() for mapping in mappings]