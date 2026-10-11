from __future__ import annotations
import json
from collections import Counter
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
    return Path(__file__).resolve().parents[2] / "sensor_mapping.json"

def loadProjectMappings() -> list[SensorMapping]:
    return loadMappings(projectMappingPath())


def duplicateMappingSlots(
    mappings: list[SensorMapping],
) -> dict[tuple[str, str], list[int]]:
    """Return duplicate anatomical locations, excluding unassigned slots.

    A left and right muscle are distinct locations. The side is derived from
    the final ``-R`` or ``-L`` in the sensor abbreviation.
    """
    locations: dict[tuple[str, str], list[int]] = {}
    for mapping in mappings:
        muscle = " ".join(mapping.english_name.casefold().split())
        if not muscle or muscle == "unassigned":
            continue
        suffix = mapping.abbreviation.rsplit("-", 1)[-1].casefold()
        side = suffix if suffix in {"r", "l"} else ""
        locations.setdefault((muscle, side), []).append(mapping.slot)
    return {
        location: slots
        for location, slots in locations.items()
        if len(slots) > 1
    }


def csvColumnNames(mappings: list[SensorMapping]) -> list[str]:
    """Return readable, unique EMG CSV headers for the selected sensors."""
    names = [
        mapping.english_name.strip()
        or mapping.chinese_name.strip()
        or f"S{mapping.slot:02d}"
        for mapping in mappings
    ]
    counts = Counter(names)
    return [
        name if counts[name] == 1 else f"S{mapping.slot:02d}_{name}"
        for mapping, name in zip(mappings, names, strict=True)
    ]
