"""Identity fusion and manual character management for Phase 7."""

import math
from collections import defaultdict
from typing import Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import (
    Character,
    CharacterAssignment,
    FaceTrack,
    SpeakerTurn,
    TranscriptSegment,
    Video,
    VisualCandidate,
    new_id,
    utcnow,
)
from ..services.media import AppError
from . import voice_profiles as voice_profiles_service


def production_info() -> dict:
    return {
        "identity_scope": "project",
        "stable_ids": True,
        "auto_merge_threshold": settings.diarization_match_threshold,
        "review_threshold": settings.diarization_review_threshold,
        "match_margin": settings.diarization_match_margin,
        "manual_operations": ["rename", "merge", "split", "segment_reassignment", "visual_confirmation"],
        "face_recognition": False,
        "real_world_identity_recognition": False,
        "corrections_persisted": True,
    }


def normalize(vector: Iterable[float]) -> list[float]:
    values = [float(value) for value in vector]
    norm = math.sqrt(sum(value * value for value in values))
    if norm <= 1e-12:
        return values
    return [value / norm for value in values]


def cosine_similarity(left: Iterable[float], right: Iterable[float]) -> float:
    a = list(left)
    b = list(right)
    if not a or not b or len(a) != len(b):
        return 0.0
    return max(-1.0, min(1.0, sum(x * y for x, y in zip(a, b))))


def centroid(vectors: list[list[float]]) -> list[float]:
    usable = [normalize(vector) for vector in vectors if vector]
    if not usable:
        return []
    if len(usable) == 1:
        return usable[0]
    return normalize([sum(values) / len(values) for values in zip(*usable)])


def _weighted_centroid(existing: list[float], count: int, additions: list[list[float]]) -> tuple[list[float], int]:
    clean = [normalize(item) for item in additions if item]
    if not clean:
        return existing, count
    if not existing or count <= 0:
        return centroid(clean), len(clean)
    weight = min(max(count, 1), 100)
    scaled = [value * weight for value in normalize(existing)]
    for vector in clean:
        scaled = [a + b for a, b in zip(scaled, vector)]
    return normalize(scaled), count + len(clean)


def voice_characteristics(character: Character, turns: list[SpeakerTurn] | None = None) -> dict:
    items = list(turns or [])
    embedding = character.voice_embedding_json or []
    durations = [max(0, turn.end_ms - turn.start_ms) for turn in items]
    confidences = [turn.confidence for turn in items if turn.confidence is not None]
    return {
        "embedding_dimensions": len(embedding),
        "embedding_norm": round(math.sqrt(sum(value * value for value in embedding)), 4),
        "voice_samples": character.voice_sample_count,
        "turn_count": len(items),
        "total_speech_ms": sum(durations),
        "average_turn_ms": round(sum(durations) / len(durations), 1) if durations else 0,
        "average_turn_confidence": round(sum(confidences) / len(confidences), 4) if confidences else None,
        "auto_match_threshold": settings.diarization_match_threshold,
        "review_threshold": settings.diarization_review_threshold,
        "interpretable": False,
    }


def _next_character_name(db: Session, project_id: str) -> str:
    count = db.scalar(select(func.count()).select_from(Character).where(Character.project_id == project_id)) or 0
    return f"Character {count + 1:02d}"


def _character_embedding(character: Character) -> list[float]:
    return normalize(character.voice_embedding_json or [])


def _turn_embedding(turn) -> list[float]:
    """Read the voice embedding from either an ORM row or a detection dataclass."""
    value = getattr(turn, "embedding_json", None)
    if value is None:
        value = getattr(turn, "embedding", None)
    return normalize(value or [])


def _match_existing(
    embedding: list[float], characters: list[Character]
) -> tuple[Character | None, float, float, str]:
    scored = [
        (character, cosine_similarity(embedding, _character_embedding(character)))
        for character in characters
        if _character_embedding(character)
    ]
    if not scored:
        return None, 0.0, 0.0, "new"
    scored.sort(key=lambda item: item[1], reverse=True)
    best, best_score = scored[0]
    second_score = scored[1][1] if len(scored) > 1 else 0.0
    margin = best_score - second_score
    if best_score >= settings.diarization_match_threshold and margin >= settings.diarization_match_margin:
        return best, best_score, margin, "auto"
    if best_score >= settings.diarization_review_threshold:
        return best, best_score, margin, "review"
    return None, best_score, margin, "new"


def fuse_turns(
    db: Session,
    video_id: str,
    transcript_id: str | None,
    turns: list[SpeakerTurn],
) -> dict:
    """Assign speaker turns to stable project-scoped character IDs.

    Similar voices are not silently merged: close matches below the configured
    margin are marked for review and remain manually editable.
    """
    video = db.get(Video, video_id)
    if not video:
        raise AppError(404, "VIDEO_NOT_FOUND", "Video for this detection no longer exists")
    characters = list(
        db.scalars(
            select(Character)
            .where(Character.project_id == video.project_id)
            .order_by(Character.created_at, Character.id)
        )
    )
    by_label: dict[str, list[SpeakerTurn]] = defaultdict(list)
    for turn in turns:
        by_label[turn.label].append(turn)

    created: list[Character] = []
    decisions: list[dict] = []
    for label, group in sorted(by_label.items()):
        group.sort(key=lambda turn: (turn.start_ms, turn.end_ms))
        group_embedding = centroid([_turn_embedding(turn) for turn in group])
        match, score, margin, decision = _match_existing(group_embedding, characters)
        if match is None:
            match = Character(
                id=new_id(),
                project_id=video.project_id,
                name=_next_character_name(db, video.project_id),
                source_label=label,
                status="review",
                confidence=round(max(0.0, min(1.0, score)), 4),
                notes=(
                    f"Possible match to a known voice (similarity {score:.3f}, margin {margin:.3f}); review before merging."
                    if score >= settings.diarization_review_threshold
                    else None
                ),
                voice_embedding_json=group_embedding,
                voice_sample_count=len(group),
            )
            db.add(match)
            db.flush()
            created.append(match)
            decisions.append(
                {
                    "label": label,
                    "character_id": match.id,
                    "decision": "new",
                    "similarity": round(score, 4),
                    "margin": round(margin, 4),
                }
            )
        else:
            match.voice_embedding_json, match.voice_sample_count = _weighted_centroid(
                _character_embedding(match), match.voice_sample_count, [_turn_embedding(turn) for turn in group]
            )
            if decision == "review":
                match.status = "review"
                match.notes = (
                    (match.notes or "")
                    + f"\nReview: {label} is close to this character (similarity {score:.3f}, margin {margin:.3f})."
                ).strip()
            match.confidence = round(max(0.0, min(1.0, score)), 4)
            decisions.append(
                {
                    "label": label,
                    "character_id": match.id,
                    "decision": decision,
                    "similarity": round(score, 4),
                    "margin": round(margin, 4),
                }
            )
        for turn in group:
            turn.character_id = match.id
            turn.assignment_source = decision
            turn.reviewed = decision == "auto"
            if turn.confidence is not None and decision == "review":
                turn.confidence = round(min(float(turn.confidence), max(0.0, min(1.0, score))), 4)
        if match not in characters:
            characters.append(match)

    db.flush()
    return {"decisions": decisions, "characters": characters, "created": created}


def assign_transcript_segments(
    db: Session,
    video_id: str,
    transcript_id: str | None,
    turns: list[SpeakerTurn],
    source: str = "auto_fusion",
) -> int:
    if not transcript_id:
        return 0
    segments = list(
        db.scalars(
            select(TranscriptSegment)
            .where(TranscriptSegment.transcript_id == transcript_id)
            .order_by(TranscriptSegment.position)
        )
    )
    assigned = 0
    for segment in segments:
        overlaps: list[tuple[float, SpeakerTurn]] = []
        for turn in turns:
            start = max(segment.start_ms, turn.start_ms)
            end = min(segment.end_ms, turn.end_ms)
            if end > start:
                overlap = end - start
                denominator = max(1, segment.end_ms - segment.start_ms)
                overlaps.append((overlap / denominator, turn))
        if not overlaps:
            continue
        overlaps.sort(key=lambda item: item[0], reverse=True)
        ratio, turn = overlaps[0]
        confidence = round(max(0.0, min(1.0, ratio * (turn.confidence or 1.0))), 4)
        existing = db.scalar(select(CharacterAssignment).where(CharacterAssignment.segment_id == segment.id))
        if existing is not None and (existing.reviewed or str(existing.source).startswith("user")):
            continue
        if existing is None:
            existing = CharacterAssignment(
                id=new_id(),
                character_id=turn.character_id,
                segment_id=segment.id,
                video_id=video_id,
                source=source,
                confidence=confidence,
                reviewed=False,
            )
            db.add(existing)
        else:
            existing.character_id = turn.character_id
            existing.source = source
            existing.confidence = confidence
            existing.reviewed = False
            existing.updated_at = utcnow()
        assigned += 1
    db.flush()
    return assigned


def scene_count(db: Session, character_id: str) -> int:
    turn_videos = set(
        db.scalars(select(SpeakerTurn.video_id).where(SpeakerTurn.character_id == character_id))
    )
    assignment_videos = set(
        db.scalars(select(CharacterAssignment.video_id).where(CharacterAssignment.character_id == character_id))
    )
    return len(turn_videos | assignment_videos)


def merge_characters(db: Session, target: Character, source_ids: list[str], name: str | None) -> Character:
    sources = list(db.scalars(select(Character).where(Character.id.in_(source_ids))))
    missing = set(source_ids) - {item.id for item in sources}
    if missing:
        raise AppError(404, "CHARACTER_NOT_FOUND", "One or more characters to merge were not found.")
    for source in sources:
        if source.id == target.id or source.project_id != target.project_id:
            raise AppError(422, "CHARACTER_MERGE_CONFLICT", "Characters must belong to the same project and be distinct.")

        # Each character stores one centroid vector, not a list of vectors.
        source_embedding = _character_embedding(source)
        target.voice_embedding_json, target.voice_sample_count = _weighted_centroid(
            _character_embedding(target),
            target.voice_sample_count,
            [source_embedding] if source_embedding else [],
        )
        for turn in db.scalars(select(SpeakerTurn).where(SpeakerTurn.character_id == source.id)):
            turn.character_id = target.id
            turn.reviewed = True
            turn.assignment_source = "user_merge"
        for track in db.scalars(select(FaceTrack).where(FaceTrack.character_id == source.id)):
            track.character_id = target.id
        for candidate in db.scalars(select(VisualCandidate).where(VisualCandidate.character_id == source.id)):
            candidate.character_id = target.id
        for candidate in db.scalars(
            select(VisualCandidate).where(VisualCandidate.confirmed_character_id == source.id)
        ):
            candidate.confirmed_character_id = target.id
        assignments = list(
            db.scalars(select(CharacterAssignment).where(CharacterAssignment.character_id == source.id))
        )
        target_segments = set(
            db.scalars(
                select(CharacterAssignment.segment_id).where(CharacterAssignment.character_id == target.id)
            )
        )
        for assignment in assignments:
            if assignment.segment_id in target_segments:
                db.delete(assignment)
                continue
            assignment.character_id = target.id
            assignment.source = "user_merge"
            assignment.reviewed = True
        # Flush the reassignments, then expire the stale relationship
        # collections so deleting the source does not null out rows that now
        # point at the target character.
        db.flush()
        db.expire(source, ["speaker_turns", "face_tracks", "assignments"])
        db.delete(source)
    voice_profiles_service.on_merge(db, target, source_ids)
    if name:
        target.name = name.strip()
    target.status = "confirmed"
    target.updated_at = utcnow()
    db.flush()
    return target


def split_character(
    db: Session,
    source: Character,
    name: str | None,
    segment_ids: list[str],
    turn_ids: list[str],
    face_track_ids: list[str],
) -> Character:
    if not (segment_ids or turn_ids or face_track_ids):
        raise AppError(422, "CHARACTER_SPLIT_EMPTY", "Select at least one segment, turn or face track to split.")
    new = Character(
        id=new_id(),
        project_id=source.project_id,
        name=(name or f"{source.name} split").strip()[:120],
        source_label=source.source_label,
        status="review",
        confidence=source.confidence,
        notes=f"Split from {source.name}.",
        voice_embedding_json=[],
        voice_sample_count=0,
    )
    db.add(new)
    db.flush()

    moved_embeddings: list[list[float]] = []
    for turn_id in set(turn_ids):
        turn = db.get(SpeakerTurn, turn_id)
        if not turn or turn.character_id != source.id:
            raise AppError(422, "CHARACTER_SPLIT_CONFLICT", f"Speaker turn {turn_id} does not belong to this character.")
        turn.character_id = new.id
        turn.reviewed = False
        turn.assignment_source = "user_split"
        embedding = _turn_embedding(turn)
        if embedding:
            moved_embeddings.append(embedding)
    for segment_id in set(segment_ids):
        assignment = db.scalar(select(CharacterAssignment).where(CharacterAssignment.segment_id == segment_id))
        if not assignment or assignment.character_id != source.id:
            raise AppError(422, "CHARACTER_SPLIT_CONFLICT", f"Dialogue segment {segment_id} does not belong to this character.")
        assignment.character_id = new.id
        assignment.source = "user_split"
        assignment.reviewed = False
        assignment.updated_at = utcnow()
        for candidate in db.scalars(
            select(VisualCandidate).where(VisualCandidate.confirmed_character_id == source.id)
        ):
            if candidate.segment_id == segment_id:
                candidate.confirmed_character_id = new.id
                candidate.character_id = new.id
    for track_id in set(face_track_ids):
        track = db.get(FaceTrack, track_id)
        if not track or track.character_id != source.id:
            raise AppError(422, "CHARACTER_SPLIT_CONFLICT", f"Face track {track_id} does not belong to this character.")
        track.character_id = new.id
        for candidate in db.scalars(select(VisualCandidate).where(VisualCandidate.face_track_id == track.id)):
            if candidate.confirmed_character_id == source.id:
                candidate.confirmed_character_id = new.id
                candidate.character_id = new.id

    new.voice_embedding_json = centroid(moved_embeddings)
    new.voice_sample_count = len(moved_embeddings)
    remaining = list(
        db.scalars(select(SpeakerTurn).where(SpeakerTurn.character_id == source.id))
    )
    if remaining:
        source.voice_embedding_json = centroid([_turn_embedding(turn) for turn in remaining if _turn_embedding(turn)])
        source.voice_sample_count = len(remaining)
    source.updated_at = utcnow()
    voice_profiles_service.on_split(db, source, new)
    db.flush()
    return new
