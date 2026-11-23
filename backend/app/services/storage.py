import shutil
import uuid
from pathlib import Path

from ..config import settings


class StorageService:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or (settings.data_dir / "artifacts")
        self.root.mkdir(parents=True, exist_ok=True)

    def project_dir(self, project_id: str) -> Path:
        path = self.root / "projects" / project_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def video_key(self, project_id: str, suffix: str) -> str:
        safe_suffix = "".join(ch for ch in suffix.lower() if ch.isalnum() or ch in ". -_")
        safe_suffix = safe_suffix.lstrip(".").replace(" ", "_")[-40:] or "video"
        return f"projects/{project_id}/source/{uuid.uuid4().hex}.{safe_suffix}"

    def asset_key(self, project_id: str, video_id: str, kind: str, version: int, filename: str) -> str:
        safe_kind = "".join(ch for ch in kind.lower() if ch.isalnum() or ch in "-_") or "asset"
        safe_name = "".join(ch for ch in filename.lower() if ch.isalnum() or ch in ".-_") or "asset.bin"
        return f"projects/{project_id}/probes/{video_id}/{safe_kind}/v{version}/{safe_name}"

    def upload_dir_key(self, project_id: str, session_id: str) -> str:
        return f"projects/{project_id}/uploads/{session_id}"

    def upload_part_key(self, project_id: str, session_id: str, index: int) -> str:
        return f"{self.upload_dir_key(project_id, session_id)}/parts/{index:06d}.part"

    def absolute_path(self, storage_key: str) -> Path:
        candidate = (self.root / storage_key).resolve()
        if not candidate.is_relative_to(self.root.resolve()):
            raise ValueError("Invalid storage key")
        return candidate

    def save(self, storage_key: str, source_path: Path) -> None:
        destination = self.absolute_path(storage_key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source_path), str(destination))

    def write_bytes(self, storage_key: str, payload: bytes) -> int:
        destination = self.absolute_path(storage_key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        return len(payload)

    def delete_upload_session(self, project_id: str, session_id: str) -> None:
        target = self.absolute_path(self.upload_dir_key(project_id, session_id))
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)

    def delete_project(self, project_id: str) -> None:
        target = self.root / "projects" / project_id
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)


storage = StorageService()
