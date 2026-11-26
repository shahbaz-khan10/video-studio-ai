from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db
from ..schemas import AudioMixSettingsOut, AudioMixSettingsUpdate
from ..services.mixer import read_settings, reset_settings, save_settings

router = APIRouter(prefix="/projects", tags=["audio-mixer"])


@router.get("/{project_id}/audio-mix", response_model=AudioMixSettingsOut)
def get_audio_mix_settings(project_id: str, db: Session = Depends(get_db)) -> AudioMixSettingsOut:
    return AudioMixSettingsOut.model_validate(read_settings(db, project_id))


@router.put("/{project_id}/audio-mix", response_model=AudioMixSettingsOut)
def update_audio_mix_settings(
    project_id: str,
    payload: AudioMixSettingsUpdate,
    db: Session = Depends(get_db),
) -> AudioMixSettingsOut:
    return AudioMixSettingsOut.model_validate(save_settings(db, project_id, payload.model_dump()))


@router.post("/{project_id}/audio-mix/reset", response_model=AudioMixSettingsOut)
def reset_audio_mix_settings(project_id: str, db: Session = Depends(get_db)) -> AudioMixSettingsOut:
    return AudioMixSettingsOut.model_validate(reset_settings(db, project_id))
