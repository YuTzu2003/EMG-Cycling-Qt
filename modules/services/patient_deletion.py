"""Delete a patient and their tests, including application-managed local data."""
from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from modules.services import analysis_cache
from modules.services.database import connect
from modules.services.normalized_sessions import _source_root


@dataclass
class PatientDeletion:
    test_ids: list[str]
    warnings: list[str] = field(default_factory=list)


def delete_patient(patient_id: str) -> PatientDeletion | None:
    """Stage owned directories before SQL deletion; restore them on rollback.

    Paths supplied by SourceFiles never grant permission to delete arbitrary
    desktop exports or another test's files. Only per-test source directories
    and hashed analysis-cache directories belong to this application.
    """
    source_root = _source_root().resolve()
    cache_root = analysis_cache.CACHE_ROOT.resolve()
    staged: list[tuple[Path, Path]] = []
    result = PatientDeletion([])

    def stage_directory(path: Path, root: Path) -> None:
        if not path.exists():
            return
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()) or path.resolve().parent != root:
            raise ValueError("偵測到不安全的測試檔案位置，已取消刪除。")
        # Rename within the same filesystem, so rollback can restore the folder.
        staging = root / f".patient-delete-{uuid4()}"
        path.rename(staging)
        staged.append((path, staging))

    with connect() as db:
        cursor = db.cursor()
        try:
            cursor.execute("SELECT PatientID FROM dbo.Patients WITH (UPDLOCK, HOLDLOCK) WHERE PatientID=%s", (patient_id,))
            if not cursor.fetchone():
                db.rollback()
                return None
            cursor.execute("SELECT TestID FROM dbo.Sessions WITH (UPDLOCK, HOLDLOCK) WHERE PatientID=%s", (patient_id,))
            result.test_ids = [str(row[0]) for row in cursor.fetchall()]
            owned = []
            for test_id in result.test_ids:
                folder = source_root / test_id
                if folder.resolve().parent != source_root:
                    raise ValueError("測試編號無法對應安全的檔案位置，已取消刪除。")
                owned.append(folder)
            cursor.execute("""SELECT SoucePath FROM dbo.SourceFiles
                WHERE TestID IN (SELECT TestID FROM dbo.Sessions WHERE PatientID=%s)""", (patient_id,))
            for row in cursor.fetchall():
                path = Path(str(row[0])).resolve()
                if not any(path.is_relative_to(folder.resolve()) for folder in owned):
                    result.warnings.append("有來源檔位於網站管理範圍外，需另行刪除。")
                    break
            for test_id, folder in zip(result.test_ids, owned):
                stage_directory(folder, source_root)
                cache_folder = cache_root / hashlib.sha256(test_id.encode("utf-8")).hexdigest()
                stage_directory(cache_folder, cache_root)
            for table in ("EMGData", "PedalData", "TrainerData", "TestBaselines", "SourceFiles"):
                cursor.execute(f"DELETE FROM dbo.{table} WHERE TestID IN (SELECT TestID FROM dbo.Sessions WHERE PatientID=%s)", (patient_id,))
            cursor.execute("DELETE FROM dbo.Sessions WHERE PatientID=%s", (patient_id,))
            cursor.execute("DELETE FROM dbo.Patients WHERE PatientID=%s", (patient_id,))
            db.commit()
        except Exception:
            db.rollback()
            for original, staging in reversed(staged):
                staging.rename(original)
            raise

    for _original, staging in staged:
        try:
            shutil.rmtree(staging)
        except OSError:
            result.warnings.append(f"部分本地檔案未能清除：{staging}")
    return result
