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

def defaultMappings() -> list[SensorMapping]:
    return [SensorMapping(slot, "未指定", "Unassigned", "") for slot in range(1, CHANNEL_COUNT + 1)]

def loadMappings(path: Path) -> list[SensorMapping]:
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
    return Path(__file__).resolve().parents[3] / "sensor_mapping.json"

def loadProjectMappings() -> list[SensorMapping]:
    return loadMappings(projectMappingPath())

def duplicateMappingSlots(mappings: list[SensorMapping],) -> dict[tuple[str, str], list[int]]:
    locations: dict[tuple[str, str], list[int]] = {}
    for mapping in mappings:
        muscle = " ".join(mapping.english_name.casefold().split())
        if not muscle or muscle == "unassigned":
            continue
        suffix = mapping.abbreviation.rsplit("-", 1)[-1].casefold()
        side = suffix if suffix in {"r", "l"} else ""
        locations.setdefault((muscle, side), []).append(mapping.slot)
    return {location: slots
        for location, slots in locations.items()
        if len(slots) > 1
    }


def csvColumnNames(mappings: list[SensorMapping]) -> list[str]:
    columns = []
    for mapping in mappings:
        name = mapping.english_name.strip() or mapping.chinese_name.strip() or "Unassigned"
        suffix = mapping.abbreviation.upper().rsplit("-", 1)[-1]
        side = f"_{suffix}" if suffix in {"L", "R"} else ""
        columns.append(f"S{mapping.slot:02d}_{name}{side}")
    return columns

def matchesCsvColumn(mapping: SensorMapping, column: str) -> bool:
    english = mapping.english_name.strip()
    candidates = {csvColumnNames([mapping])[0], f"S{mapping.slot:02d}_{english}",
                  english, mapping.abbreviation.strip()}
    return column.strip().casefold() in {candidate.casefold() for candidate in candidates if candidate}


def recordingChannelSides(emg_path: Path, columns: list[str],mappings: list[SensorMapping]) -> tuple[dict[str, str], str]:
    companion = emg_path.with_suffix(".analysis.json")
    saved: dict = {}
    try:
        document = json.loads(companion.read_text(encoding="utf-8"))
        if document.get("raw_files", {}).get("emg") == emg_path.name:
            saved = document.get("channels", {})
    except (OSError, ValueError, TypeError):
        pass
    sides: dict[str, str] = {}
    sources = set()
    for column in columns:
        match = re.match(r"^S(\d+)_", column, flags=re.IGNORECASE)
        slot = str(int(match.group(1))) if match else None
        record = saved.get(slot, {}) if slot else {}
        abbreviation = str(record.get("mapping_name", "")).rsplit("|", 1)[-1].upper()
        recorded_side = abbreviation.rsplit("-", 1)[-1] if "-" in abbreviation else ""
        header_match = re.search(r"_([RL])$", column, flags=re.IGNORECASE)
        header_side = header_match.group(1).upper() if header_match else ""
        if recorded_side in {"L", "R"} and header_side in {"L", "R"} and recorded_side != header_side:
            raise ValueError(f"{column} 的 CSV 側別與錄製時對應不一致。")
        mapping = next((item for item in mappings if matchesCsvColumn(item, column)), None)
        mapped = mapping.abbreviation.upper().rsplit("-", 1)[-1] if mapping and "-" in mapping.abbreviation else ""
        for value, source in ((recorded_side, "錄製時對應"), (header_side, "CSV 欄名"), (mapped, "目前設定")):
            if value in {"L", "R"}:
                sides[column] = value
                sources.add(source)
                break
    return sides, "、".join(sorted(sources)) if sources else "無側別資料"