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


def test2LegacyMappings() -> list[SensorMapping]:
    """The sensor placement used for the existing Test2 recordings.

    Keep this separately from the live mapping: CSV headers from Test2 only
    contain a slot and an old muscle name, so interpreting them with a later
    placement plan would silently assign the measurements to wrong muscles.
    """
    rows = (
        ("右股二頭肌", "Biceps femoris", "BF-R"),
        ("左股二頭肌", "Biceps femoris", "BF-L"),
        ("右股內側肌", "Vastus medialis", "VM-R"),
        ("左股內側肌", "Vastus medialis", "VM-L"),
        ("右股直肌", "Rectus femoris", "RF-R"),
        ("左股直肌", "Rectus femoris", "RF-L"),
        ("右股外側肌", "Vastus lateralis", "VL-R"),
        ("左股外側肌", "Vastus lateralis", "VL-L"),
        ("未指定", "Unassigned", ""),
        ("右脛前肌", "Tibialis anterior", "TA-R"),
        ("左脛前肌", "Tibialis anterior", "TA-L"),
        ("右腓腸肌", "Gastrocnemius", "GAS-R"),
        ("左腓腸肌", "Gastrocnemius", "GAS-L"),
        ("右比目魚肌", "Soleus", "SOL-R"),
        ("未指定", "Unassigned", ""),
        ("左比目魚肌", "Soleus", "SOL-L"),
    )
    return [SensorMapping(slot, *row) for slot, row in enumerate(rows, 1)]


def futureMeasurementMappings() -> list[SensorMapping]:
    """Placement confirmed for measurements collected after Test2."""
    rows = (
        ("右股內側肌", "Vastus medialis", "VM-R"),
        ("右股直肌", "Rectus femoris", "RF-R"),
        ("右股外側肌", "Vastus lateralis", "VL-R"),
        ("左股內側肌", "Vastus medialis", "VM-L"),
        ("左股直肌", "Rectus femoris", "RF-L"),
        ("左股外側肌", "Vastus lateralis", "VL-L"),
        ("右脛前肌", "Tibialis anterior", "TA-R"),
        ("左脛前肌", "Tibialis anterior", "TA-L"),
        ("未指定", "Unassigned", ""),
        ("右股二頭肌", "Biceps femoris", "BF-R"),
        ("左股二頭肌", "Biceps femoris", "BF-L"),
        ("右比目魚肌", "Soleus", "SOL-R"),
        ("右腓腸肌", "Gastrocnemius", "GAS-R"),
        ("左比目魚肌", "Soleus", "SOL-L"),
        ("未指定", "Unassigned", ""),
        ("左腓腸肌", "Gastrocnemius", "GAS-L"),
    )
    return [SensorMapping(slot, *row) for slot, row in enumerate(rows, 1)]

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
        # Keep the hardware slot first, followed by anatomical side, for
        # example ``S01_R_Biceps femoris``.  This stays easy to sort by slot
        # while keeping the side visible in every external CSV viewer.
        side = f"_{suffix}" if suffix in {"L", "R"} else ""
        columns.append(f"S{mapping.slot:02d}{side}_{name}")
    return columns

def matchesCsvColumn(mapping: SensorMapping, column: str) -> bool:
    english = mapping.english_name.strip()
    side = mapping.abbreviation.upper().rsplit("-", 1)[-1]
    legacy_side = f"S{mapping.slot:02d}_{english}_{side}" if side in {"L", "R"} else ""
    prior_prefixed = f"{side}_S{mapping.slot:02d}_{english}" if side in {"L", "R"} else ""
    candidates = {csvColumnNames([mapping])[0], f"S{mapping.slot:02d}_{english}", legacy_side, prior_prefixed,
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
        match = re.match(r"^(?:([LR])_)?S(\d+)(?:_([LR]))?_", column, flags=re.IGNORECASE)
        prefix_side = match.group(1).upper() if match and match.group(1) else ""
        slot = str(int(match.group(2))) if match else None
        inline_side = match.group(3).upper() if match and match.group(3) else ""
        record = saved.get(slot, {}) if slot else {}
        abbreviation = str(record.get("mapping_name", "")).rsplit("|", 1)[-1].upper()
        recorded_side = abbreviation.rsplit("-", 1)[-1] if "-" in abbreviation else ""
        suffix_match = re.search(r"_([RL])$", column, flags=re.IGNORECASE)
        suffix_side = suffix_match.group(1).upper() if suffix_match else ""
        if prefix_side and inline_side and prefix_side != inline_side:
            raise ValueError(f"{column} 的 CSV 欄名包含互相矛盾的左右側別。")
        if prefix_side and suffix_side and prefix_side != suffix_side:
            raise ValueError(f"{column} 的 CSV 欄名包含互相矛盾的左右側別。")
        if inline_side and suffix_side and inline_side != suffix_side:
            raise ValueError(f"{column} 的 CSV 欄名包含互相矛盾的左右側別。")
        header_side = inline_side or prefix_side or suffix_side
        mapping = next((item for item in mappings if matchesCsvColumn(item, column)), None)
        mapped = mapping.abbreviation.upper().rsplit("-", 1)[-1] if mapping and "-" in mapping.abbreviation else ""
        declared = {value for value in (recorded_side, header_side, mapped) if value in {"L", "R"}}
        if len(declared) > 1:
            raise ValueError(f"{column} 的 CSV、錄製時對應與目前設定有左右側別衝突。")
        for value, source in ((recorded_side, "錄製時對應"), (header_side, "CSV 欄名"), (mapped, "目前設定")):
            if value in {"L", "R"}:
                sides[column] = value
                sources.add(source)
                break
    return sides, "、".join(sorted(sources)) if sources else "無側別資料"
