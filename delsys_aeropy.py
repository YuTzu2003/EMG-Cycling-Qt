"""Optional DelsysAPI/AeroPy bootstrap using credential files.

This is separate from the license-free SDK Server TCP client. It requires the
vendor-provided DelsysAPI.dll and pythonnet runtime before it can be used.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


PROJECT_DIR = Path(__file__).resolve().parent


def load_credentials(
    key_path: Path = PROJECT_DIR / "DelsysAPI.key",
    license_path: Path = PROJECT_DIR / "DelsysAPI.lic",
) -> tuple[str, str]:
    """Load credentials without embedding or printing their secret contents."""
    missing = [str(path) for path in (key_path, license_path) if not path.is_file()]
    if missing:
        raise FileNotFoundError("找不到 Delsys 憑證檔：" + ", ".join(missing))
    key = key_path.read_text(encoding="utf-8").strip()
    license_text = license_path.read_text(encoding="utf-8").strip()
    if not key or not license_text:
        raise ValueError("DelsysAPI.key 或 DelsysAPI.lic 是空檔案")
    return key, license_text


def connect_aeropy(
    dll_path: Path = PROJECT_DIR / "resources" / "DelsysAPI.dll",
) -> Any:
    """Create and validate an AeroPy API instance against the USB receiver."""
    if not dll_path.is_file():
        raise FileNotFoundError(
            f"找不到 {dll_path}；請從 Delsys API 套件複製 DLL 與其相依檔。"
        )

    try:
        from pythonnet import load
    except ImportError as exc:
        raise RuntimeError("尚未安裝 pythonnet；請執行：py -m pip install pythonnet") from exc

    load("coreclr")
    import clr  # type: ignore

    clr.AddReference(str(dll_path))
    from Aero import AeroPy  # type: ignore

    key, license_text = load_credentials()
    api = AeroPy()
    api.ValidateBase(key, license_text)
    return api
