"""Phase 8 — context-aware dialogue translation (English → Urdu / Hindi).

Pipeline for each dialogue segment and target language:

1. Context: previous/next lines, scene (dialogue gap based) and scene note,
   speaker character + translation profile, likely listener, relationship,
   register cues, emotional tone, humour cues.
2. Pre-edit the English: adapt known idioms (native equivalent or plain
   meaning, never literal), lock character names / glossary terms behind
   placeholders, split into sentences.
3. Sentence-level MT with an open-source model (translation_engine).
4. Post-edit: restore locked names in fixed target spellings, register
   (aap / tum / tu), speaker and listener gender agreement, punctuation.
5. Optional: self-hosted open LLM refinement with the full context.
6. Roman Urdu, quality flags, dubbing-fit estimate, persistence that never
   silently overwrites a manual edit.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..config import settings
from ..database import SessionLocal
from ..models import (
    Character,
    CharacterAssignment,
    CharacterRelationship,
    CharacterTranslationProfile,
    ProcessingJob,
    SegmentTranslation,
    Transcript,
    TranscriptSegment,
    TranslationGlossaryTerm,
    TranslationSceneNote,
    Video,
    utcnow,
)
from . import translation_engine as engine
from . import translation_lang as lang
from .media import AppError

ActiveCheck = Callable[[], None]
LANGUAGES = ("ur", "hi")
HISTORY_LIMIT = 30
AUTO_CHARACTER_NAME = re.compile(r"^(character|speaker|unknown|person)\s*[\w-]{0,4}$", re.I)

TRANSLATION_STEP_NAMES = [
    "load_transcript",
    "prepare_context",
    "load_model",
    "translate",
    "quality_check",
    "finalize",
]


# ---------------------------------------------------------------------------
# Idioms: native equivalents where a noun-phrase idiom exists in Urdu/Hindi,
# otherwise the plain meaning is translated instead of the literal words.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Idiom:
    pattern: str
    plain: str
    ur: str | None = None
    hi: str | None = None


IDIOMS: list[Idiom] = [
    Idiom(r"\ba piece of cake\b", "very easy", "بائیں ہاتھ کا کھیل", "बाएँ हाथ का खेल"),
    Idiom(r"\bpiece of cake\b", "very easy", "بائیں ہاتھ کا کھیل", "बाएँ हाथ का खेल"),
    Idiom(r"\bon cloud nine\b", "extremely happy", "ساتویں آسمان پر", "सातवें आसमान पर"),
    Idiom(r"\bover the moon\b", "extremely happy", "ساتویں آسمان پر", "सातवें आसमान पर"),
    Idiom(r"\bin hot water\b", "in trouble", "مصیبت میں", "मुसीबत में"),
    Idiom(r"\ba dime a dozen\b", "very common", "ٹکے سیر", "टके सेर"),
    Idiom(r"\bonce in a blue moon\b", "very rarely", "کبھی کبھار", "कभी-कभार"),
    Idiom(r"\bout of the blue\b", "suddenly", "اچانک", "अचानक"),
    Idiom(r"\bhead over heels\b", "deeply in love", None, None),
    Idiom(r"\bbreak a leg\b", "best of luck", None, None),
    Idiom(r"\bunder the weather\b", "a little sick", None, None),
    Idiom(r"\bcosts? an arm and a leg\b", "is very expensive", None, None),
    Idiom(r"\bhit the (sack|hay)\b", "go to sleep", None, None),
    Idiom(r"\bspill(ed|ing)? the beans\b", "reveal the secret", None, None),
    Idiom(r"\blet the cat out of the bag\b", "revealed the secret", None, None),
    Idiom(r"\bbite the bullet\b", "face it bravely", None, None),
    Idiom(r"\bbeat(ing)? around the bush\b", "avoiding the real point", None, None),
    Idiom(r"\bcall it a day\b", "stop for today", None, None),
    Idiom(r"\braining cats and dogs\b", "raining very heavily", None, None),
    Idiom(r"\bkill two birds with one stone\b", "do two things at once", None, None),
    Idiom(r"\bthe ball is in your court\b", "the decision is yours now", None, None),
    Idiom(r"\bcut to the chase\b", "come to the point", None, None),
    Idiom(r"\bhang in there\b", "stay strong", None, None),
    Idiom(r"\bpull(ing|ed)? my leg\b", "joking with me", None, None),
    Idiom(r"\bpull(ing|ed)? your leg\b", "joking with you", None, None),
    Idiom(r"\bbreak the ice\b", "start the conversation", None, None),
    Idiom(r"\bthe last straw\b", "the final limit", None, None),
    Idiom(r"\bmy bad\b", "my mistake", None, None),
    Idiom(r"\bwhat'?s up\b", "how are you", None, None),
    Idiom(r"\bchill out\b", "calm down", None, None),
    Idiom(r"\bi'?m all ears\b", "I am listening carefully", None, None),
    Idiom(r"\bcold feet\b", "nervous and afraid", None, None),
    Idiom(r"\bgo the extra mile\b", "make extra effort", None, None),
    Idiom(r"\bback to square one\b", "back to the very beginning", None, None),
    Idiom(r"\beasier said than done\b", "easy to say but hard to do", None, None),
    Idiom(r"\bspeak of the devil\b", "we were just talking about you", None, None),
    Idiom(r"\bhold your horses\b", "wait, be patient", None, None),
    Idiom(r"\byou'?re toast\b", "you are finished", None, None),
    Idiom(r"\bhit the road\b", "leave now", None, None),
    Idiom(r"\bkeep an eye on\b", "watch carefully", None, None),
    Idiom(r"\bget cold feet\b", "become nervous and back out", None, None),
    Idiom(r"\bno big deal\b", "not a problem", None, None),
    Idiom(r"\byou bet\b", "certainly", None, None),
    Idiom(r"\bknock it off\b", "stop it", None, None),
    Idiom(r"\bget a grip\b", "control yourself", None, None),
    Idiom(r"\bfor crying out loud\b", "for heaven's sake", None, None),
]
_IDIOM_RX = [(re.compile(item.pattern, re.I), item) for item in IDIOMS]

FORMAL_CUES = re.compile(r"\b(sir|madam|ma'?am|mr\.?|mrs\.?|your honou?r|officer|doctor|professor|boss|uncle|aunty|auntie)\b", re.I)
INFORMAL_CUES = re.compile(r"\b(bro|dude|buddy|mate|pal|man,|kiddo|yaar|guys)\b", re.I)
TONE_LEXICON = {
    "angry": re.compile(r"\b(shut up|get out|idiot|damn|hate|how dare|stupid|liar|enough)\b", re.I),
    "sad": re.compile(r"\b(sorry|miss you|cry|crying|died|dead|lost|alone|goodbye)\b", re.I),
    "fearful": re.compile(r"\b(help|afraid|scared|run|hide|please don'?t)\b", re.I),
    "affectionate": re.compile(r"\b(love|darling|sweetheart|dear|honey|miss you)\b", re.I),
    "happy": re.compile(r"\b(great|wonderful|amazing|haha|yay|congratulations|awesome)\b", re.I),
}


def _utc() -> datetime:
    return datetime.now(timezone.utc)


def detect_tone(text: str) -> str:
    stripped = text.strip()
    letters = re.sub(r"[^A-Za-z]", "", stripped)
    if len(letters) >= 4 and letters.isupper():
        return "shouting"
    for tone, pattern in TONE_LEXICON.items():
        if pattern.search(stripped):
            return tone
    if stripped.endswith("!"):
        return "excited"
    if stripped.endswith("?"):
        return "questioning"
    return "neutral"


def split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?…])\s+(?=[\"'“A-Z0-9¿])", text.strip())
    return [part.strip() for part in parts if part.strip()]


# ---------------------------------------------------------------------------
# Name transliteration fallback (only when MT cannot give a native spelling)
# ---------------------------------------------------------------------------

_HI_CONS = {
    "sh": "श", "ch": "च", "th": "थ", "kh": "ख", "ph": "फ", "gh": "घ", "bh": "भ", "dh": "ध",
    "b": "ब", "c": "क", "d": "ड", "f": "फ", "g": "ग", "h": "ह", "j": "ज", "k": "क", "l": "ल",
    "m": "म", "n": "न", "p": "प", "q": "क", "r": "र", "s": "स", "t": "ट", "v": "व", "w": "व",
    "x": "क्स", "y": "य", "z": "ज़",
}
_HI_VOWEL_INIT = {"aa": "आ", "ee": "ई", "oo": "ऊ", "ai": "ऐ", "au": "औ", "a": "अ", "e": "ए", "i": "इ", "o": "ओ", "u": "उ"}
_HI_MATRA = {"aa": "ा", "ee": "ी", "oo": "ू", "ai": "ै", "au": "ौ", "a": "", "e": "े", "i": "ि", "o": "ो", "u": "ु"}
_UR_CONS = {
    "sh": "ش", "ch": "چ", "th": "تھ", "kh": "خ", "ph": "ف", "gh": "غ", "bh": "بھ", "dh": "دھ",
    "b": "ب", "c": "ک", "d": "ڈ", "f": "ف", "g": "گ", "h": "ہ", "j": "ج", "k": "ک", "l": "ل",
    "m": "م", "n": "ن", "p": "پ", "q": "ق", "r": "ر", "s": "س", "t": "ٹ", "v": "و", "w": "و",
    "x": "کس", "y": "ی", "z": "ز",
}
_UR_VOWEL_INIT = {"aa": "آ", "ee": "ای", "oo": "او", "ai": "اے", "au": "او", "a": "ا", "e": "ای", "i": "ا", "o": "او", "u": "ا"}
_UR_VOWEL_MED = {"aa": "ا", "ee": "ی", "oo": "و", "ai": "ے", "au": "و", "a": "", "e": "ی", "i": "ی", "o": "و", "u": "و"}


def _tokens_for_name(word: str) -> list[tuple[str, str]]:
    word = word.lower()
    out: list[tuple[str, str]] = []
    index = 0
    while index < len(word):
        two = word[index : index + 2]
        if two in {"aa", "ee", "oo", "ai", "au"}:
            out.append(("v", two)); index += 2; continue
        if two in {"sh", "ch", "th", "kh", "ph", "gh", "bh", "dh"}:
            out.append(("c", two)); index += 2; continue
        char = word[index]
        if char in "aeiou":
            out.append(("v", char))
        elif char.isalpha():
            if out and out[-1] == ("c", char):
                index += 1
                continue  # collapse doubled consonants
            out.append(("c", char))
        index += 1
    return out


def transliterate_name(name: str, language: str) -> str:
    words = []
    for word in re.findall(r"[A-Za-z]+", name):
        tokens = _tokens_for_name(word)
        out = ""
        for position, (kind, value) in enumerate(tokens):
            last = position == len(tokens) - 1
            if language == "hi":
                if kind == "v":
                    if position == 0 or tokens[position - 1][0] == "v":
                        out += _HI_VOWEL_INIT[value]
                    else:
                        out += "ा" if (value == "a" and last) else ("ी" if (value == "e" and last and len(tokens) > 3) else _HI_MATRA[value])
                else:
                    if value == "y" and last and position > 0:
                        out += "ी"
                        continue
                    if position > 0 and tokens[position - 1][0] == "c":
                        out += "्"
                    out += _HI_CONS.get(value, "")
            else:
                if kind == "v":
                    if position == 0:
                        out += _UR_VOWEL_INIT[value]
                    elif tokens[position - 1][0] == "v":
                        out += "ئ" + _UR_VOWEL_MED.get(value, "")
                    else:
                        if last and value in {"a", "aa"}:
                            out += "ا" if value == "aa" or len(tokens) > 2 else "ہ"
                        elif last and value == "e":
                            out += "ے"
                        else:
                            out += _UR_VOWEL_MED[value]
                else:
                    if value == "y" and last and position > 0:
                        out += "ی"
                        continue
                    out += _UR_CONS.get(value, "")
        words.append(out)
    return " ".join(words)


# ---------------------------------------------------------------------------
# Dialogue context
# ---------------------------------------------------------------------------


@dataclass
class Speaker:
    character_id: str | None
    name: str | None
    label: str | None
    gender: str = "unknown"
    register: str = "auto"
    style: str | None = None


@dataclass
class DialogueLine:
    segment: TranscriptSegment
    speaker: Speaker
    scene_index: int
    scene_start_id: str


@dataclass
class ProjectContext:
    lines: list[DialogueLine]
    by_id: dict[str, int]
    characters: dict[str, Character]
    profiles: dict[str, CharacterTranslationProfile]
    relationships: dict[tuple[str, str], CharacterRelationship]
    glossary: list[TranslationGlossaryTerm]
    scene_notes: dict[str, str]
    memory: dict[tuple[str, str, str | None], tuple[str, str]] = field(default_factory=dict)


def latest_transcript(db: Session, video_id: str) -> Transcript | None:
    return db.scalar(
        select(Transcript)
        .where(Transcript.video_id == video_id)
        .order_by(Transcript.version.desc(), Transcript.created_at.desc())
    )


def get_or_create_profile(db: Session, character: Character) -> CharacterTranslationProfile:
    profile = db.scalar(
        select(CharacterTranslationProfile).where(CharacterTranslationProfile.character_id == character.id)
    )
    if profile is None:
        profile = CharacterTranslationProfile(project_id=character.project_id, character_id=character.id)
        db.add(profile)
        db.flush()
    return profile


def _normalise_source(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s']", "", text.lower())).strip()


def build_context(db: Session, video: Video, transcript: Transcript) -> ProjectContext:
    segments = list(transcript.segments)
    assignments = {
        item.segment_id: item
        for item in db.scalars(
            select(CharacterAssignment).where(CharacterAssignment.segment_id.in_([s.id for s in segments]))
        )
    } if segments else {}
    characters = {
        item.id: item for item in db.scalars(select(Character).where(Character.project_id == video.project_id))
    }
    profiles = {
        item.character_id: item
        for item in db.scalars(
            select(CharacterTranslationProfile).where(CharacterTranslationProfile.project_id == video.project_id)
        )
    }
    relationships = {
        (item.speaker_character_id, item.listener_character_id): item
        for item in db.scalars(
            select(CharacterRelationship).where(CharacterRelationship.project_id == video.project_id)
        )
    }
    glossary = list(
        db.scalars(select(TranslationGlossaryTerm).where(TranslationGlossaryTerm.project_id == video.project_id))
    )
    scene_notes = {
        item.segment_id: item.note
        for item in db.scalars(select(TranslationSceneNote).where(TranslationSceneNote.video_id == video.id))
    }

    lines: list[DialogueLine] = []
    scene_index = 0
    scene_start_id = segments[0].id if segments else ""
    previous_end: int | None = None
    for segment in segments:
        if previous_end is not None and segment.start_ms - previous_end >= settings.translation_scene_gap_ms:
            scene_index += 1
            scene_start_id = segment.id
        if segment.id in scene_notes and segment.id != scene_start_id:
            # A user note on any segment starts a new scene there.
            scene_index += 1
            scene_start_id = segment.id
        previous_end = segment.end_ms
        assignment = assignments.get(segment.id)
        character = characters.get(assignment.character_id) if assignment and assignment.character_id else None
        profile = profiles.get(character.id) if character else None
        lines.append(
            DialogueLine(
                segment=segment,
                speaker=Speaker(
                    character_id=character.id if character else None,
                    name=character.name if character else None,
                    label=segment.speaker,
                    gender=(profile.gender if profile else "unknown") or "unknown",
                    register=(profile.register if profile else "auto") or "auto",
                    style=profile.speaking_style if profile else None,
                ),
                scene_index=scene_index,
                scene_start_id=scene_start_id,
            )
        )

    # Translation memory: the user's own corrections are reused for identical
    # lines by the same speaker, keeping terminology consistent across scenes.
    memory: dict[tuple[str, str, str | None], tuple[str, str]] = {}
    manual_rows = db.scalars(
        select(SegmentTranslation)
        .where(SegmentTranslation.project_id == video.project_id, SegmentTranslation.origin == "manual")
        .order_by(SegmentTranslation.updated_at)
    )
    speaker_by_segment = {
        item.segment_id: item.character_id
        for item in db.scalars(select(CharacterAssignment).where(CharacterAssignment.video_id == video.id))
    }
    for row in manual_rows:
        if row.text.strip() and row.source_text.strip():
            memory[(row.language, _normalise_source(row.source_text), speaker_by_segment.get(row.segment_id))] = (row.text, row.segment_id)

    return ProjectContext(
        lines=lines,
        by_id={line.segment.id: index for index, line in enumerate(lines)},
        characters=characters,
        profiles=profiles,
        relationships=relationships,
        glossary=glossary,
        scene_notes=scene_notes,
        memory=memory,
    )


def _named_characters(ctx: ProjectContext) -> list[Character]:
    return [item for item in ctx.characters.values() if item.name and not AUTO_CHARACTER_NAME.match(item.name.strip())]


def _vocative_listener(ctx: ProjectContext, text: str, speaker_id: str | None) -> Character | None:
    for character in _named_characters(ctx):
        if character.id == speaker_id:
            continue
        name = re.escape(character.name.strip())
        if re.search(rf"(^|[.!?]\s*){name}\s*,|,\s*{name}\s*[.!?]*$|^(hey|oh|hi|hello|listen|look)\s+{name}\b", text.strip(), re.I):
            return character
    return None


KINSHIP = {
    "mother": {"mother", "mom", "mum", "mama", "mommy", "ammi", "ammu", "maa", "maji"},
    "father": {"father", "dad", "daddy", "papa", "abbu", "abba", "baba", "pitaji"},
    "brother": {"brother", "bro", "bhai", "bhaiya", "bhaijan"},
    "sister": {"sister", "sis", "baji", "apa", "didi"},
    "grandmother": {"grandma", "granny", "grandmother", "nani", "dadi"},
    "grandfather": {"grandpa", "grandfather", "nana", "dada"},
    "uncle": {"uncle", "chacha", "mamu", "mamoo", "taya"},
    "aunt": {"aunt", "auntie", "aunty", "chachi", "khala", "phuppo", "mami"},
    "wife": {"wife", "begum"},
    "husband": {"husband"},
    "son": {"son", "beta"},
    "daughter": {"daughter", "beti"},
    "boss": {"boss", "sir", "madam", "maam"},
}


def _kinship_listener(ctx: ProjectContext, text: str, speaker_id: str) -> Character | None:
    words = {word.lower().replace("'", "") for word in re.findall(r"[A-Za-z']+", text)}
    for group, aliases in KINSHIP.items():
        if not words & aliases:
            continue
        for (speaker, listener_id), rel in ctx.relationships.items():
            if speaker != speaker_id or not rel.relation:
                continue
            relation = rel.relation.lower()
            # "daughter to mother" -> the listener is the mother.
            target = relation.split(" to ")[-1]
            if group in target or any(alias in target.split() for alias in aliases):
                return ctx.characters.get(listener_id)
    return None


def segment_context(ctx: ProjectContext, segment_id: str) -> dict:
    index = ctx.by_id[segment_id]
    line = ctx.lines[index]
    text = line.segment.text

    def describe(item: DialogueLine) -> dict:
        return {
            "segment_id": item.segment.id,
            "speaker": item.speaker.name or item.speaker.label or "Unknown",
            "text": item.segment.text,
        }

    previous = [describe(item) for item in ctx.lines[max(0, index - 2) : index] if item.scene_index == line.scene_index]
    following = [describe(item) for item in ctx.lines[index + 1 : index + 3] if item.scene_index == line.scene_index]

    listener: Character | None = _vocative_listener(ctx, text, line.speaker.character_id)
    listener_reason = "addressed by name" if listener else None
    if listener is None and line.speaker.character_id:
        listener = _kinship_listener(ctx, text, line.speaker.character_id)
        listener_reason = "kinship form of address" if listener else None
    if listener is None and line.speaker.character_id:
        before = list(reversed(ctx.lines[max(0, index - 3) : index]))
        after = ctx.lines[index + 1 : index + 3]
        # A question is usually answered by the next speaker; a reply is
        # usually addressed to whoever spoke before.
        neighbours = after + before if text.rstrip().endswith("?") else before + after
        for item in neighbours:
            if item.scene_index != line.scene_index:
                continue
            if item.speaker.character_id and item.speaker.character_id != line.speaker.character_id:
                listener = ctx.characters.get(item.speaker.character_id)
                listener_reason = "adjacent speaker in scene"
                break
    listener_profile = ctx.profiles.get(listener.id) if listener else None

    register = "neutral"
    register_reason = "model default"
    relation = None
    if line.speaker.character_id and listener:
        rel = ctx.relationships.get((line.speaker.character_id, listener.id))
        if rel:
            register = rel.address_form
            relation = rel.relation
            register_reason = f"relationship{f' ({rel.relation})' if rel.relation else ''}"
    if register_reason == "model default":
        if FORMAL_CUES.search(text):
            register, register_reason = "formal", "respectful form of address in line"
        elif INFORMAL_CUES.search(text):
            register, register_reason = "informal", "casual form of address in line"
        elif line.speaker.register in {"formal", "informal", "intimate"}:
            register, register_reason = line.speaker.register, "speaker profile"

    return {
        "scene_index": line.scene_index,
        "scene_note": ctx.scene_notes.get(line.scene_start_id),
        "previous": previous,
        "following": following,
        "speaker": {
            "character_id": line.speaker.character_id,
            "name": line.speaker.name,
            "label": line.speaker.label,
            "gender": line.speaker.gender,
            "style": line.speaker.style,
        },
        "listener": {
            "character_id": listener.id if listener else None,
            "name": listener.name if listener else None,
            "gender": (listener_profile.gender if listener_profile else "unknown") or "unknown",
            "reason": listener_reason,
        },
        "relation": relation,
        "register": register,
        "register_reason": register_reason,
        "tone": detect_tone(text),
        "humor": lang.looks_like_humor(text),
    }


# ---------------------------------------------------------------------------
# Names & glossary locking
# ---------------------------------------------------------------------------


@dataclass
class Lock:
    key: str
    source: str
    targets: dict[str, str]
    roman: str


def _carrier_name(name: str, language: str) -> str | None:
    """Ask the MT model for a native spelling through a carrier sentence."""
    try:
        output = engine.translate_texts([f"My name is {name}."], language)[0]
    except Exception:
        return None
    output = lang.normalize_target(output, language)
    pattern = r"نام\s+(.+?)\s+ہے" if language == "ur" else r"नाम\s+(.+?)\s+है"
    match = re.search(pattern, output)
    if not match:
        return None
    candidate = match.group(1).strip()
    if lang.LATIN_WORD.search(candidate) or len(candidate) > len(name) * 3 + 4:
        return None
    return candidate


def resolve_character_names(db: Session, ctx: ProjectContext, languages: list[str], use_model: bool = True) -> None:
    """Fix one target spelling per character name, stored on the profile."""
    for character in _named_characters(ctx):
        profile = ctx.profiles.get(character.id) or get_or_create_profile(db, character)
        ctx.profiles[character.id] = profile
        changed = False
        for language in languages:
            attr = f"name_{language}"
            if getattr(profile, attr):
                continue
            spelling = _carrier_name(character.name.strip(), language) if use_model else None
            setattr(profile, attr, spelling or transliterate_name(character.name.strip(), language))
            changed = True
        if not profile.name_roman:
            profile.name_roman = character.name.strip()
            changed = True
        if changed:
            profile.names_auto = True
    db.flush()


def collect_locks(ctx: ProjectContext, text: str) -> list[Lock]:
    locks: list[Lock] = []
    seen: set[str] = set()
    for term in sorted(ctx.glossary, key=lambda item: -len(item.source)):
        if not term.source.strip() or term.source.lower() in seen:
            continue
        targets = {key: value for key, value in (("ur", term.ur), ("hi", term.hi)) if value}
        if not targets:
            continue
        flags = 0 if term.case_sensitive else re.I
        if re.search(rf"(?<![\w]){re.escape(term.source)}(?![\w])", text, flags):
            locks.append(Lock(term.source, term.source, targets, term.roman or term.source))
            seen.add(term.source.lower())
    for character in sorted(_named_characters(ctx), key=lambda item: -len(item.name)):
        name = character.name.strip()
        if name.lower() in seen:
            continue
        profile = ctx.profiles.get(character.id)
        if not profile:
            continue
        targets = {key: value for key, value in (("ur", profile.name_ur), ("hi", profile.name_hi)) if value}
        if targets and re.search(rf"(?<![\w]){re.escape(name)}(?:'s)?(?![\w])", text, re.I):
            locks.append(Lock(name, name, targets, profile.name_roman or name))
            seen.add(name.lower())
    return locks


def _apply_locks(text: str, locks: list[Lock]) -> tuple[str, dict[str, Lock]]:
    mapping: dict[str, Lock] = {}
    for number, lock in enumerate(locks, start=1):
        token = f"N{number}X"  # survives NLLB 8/8; "<N1>" was often dropped in Hindi
        new_text, count = re.subn(rf"(?<![\w]){re.escape(lock.source)}(?![\w])", token, text, flags=re.I)
        if count:
            text = new_text
            mapping[token] = lock
    return text, mapping


def _apply_idioms(text: str, language: str) -> tuple[str, dict[str, str], list[dict]]:
    """Replace idioms: native equivalent behind a placeholder, else plain meaning."""
    natives: dict[str, str] = {}
    used: list[dict] = []
    for number, (rx, idiom) in enumerate(_IDIOM_RX, start=1):
        match = rx.search(text)
        if not match:
            continue
        native = idiom.ur if language == "ur" else idiom.hi
        if native:
            token = f"M{number}X"
            text = rx.sub(token, text, count=1)
            natives[token] = native
            used.append({"idiom": match.group(0), "strategy": "native", "rendering": native})
        else:
            text = rx.sub(idiom.plain, text, count=1)
            used.append({"idiom": match.group(0), "strategy": "meaning", "rendering": idiom.plain})
    return text, natives, used


_MODEL_SPELLINGS: dict[tuple[str, str], str | None] = {}


def _model_spelling(name: str, language: str) -> str | None:
    key = (name.lower(), language)
    if key not in _MODEL_SPELLINGS:
        _MODEL_SPELLINGS[key] = _carrier_name(name, language)
    return _MODEL_SPELLINGS[key]


def _repair_names(text: str, locks: dict[str, Lock], language: str) -> tuple[str, list[str]]:
    """After an unlocked translation, swap the model's own spelling of each
    name (or the Latin original) for the locked spelling; report misses."""
    missing: list[str] = []
    for lock in locks.values():
        wanted = lock.targets.get(language)
        if not wanted or wanted in text:
            continue
        variants = [lock.source, transliterate_name(lock.source, language)]
        spelling = _model_spelling(lock.source, language)
        if spelling:
            variants.insert(0, spelling)
        for variant in variants:
            if variant and re.search(re.escape(variant), text, re.I):
                text = re.sub(re.escape(variant), wanted, text, flags=re.I)
                break
        else:
            missing.append(lock.source)
    return text, missing


def _restore(text: str, tokens: dict[str, str]) -> tuple[str, list[str]]:
    missing: list[str] = []
    for token, value in tokens.items():
        pattern = rf"<?(?<![A-Za-z0-9]){re.escape(token)}(?![A-Za-z0-9])>?"
        text, count = re.subn(pattern, value, text, flags=re.I)
        if not count:
            missing.append(token)
    return text, missing


# ---------------------------------------------------------------------------
# Core translation of a batch of segments
# ---------------------------------------------------------------------------


@dataclass
class Draft:
    segment_id: str
    language: str
    text: str
    raw_mt: str
    flags: list[dict]
    context: dict
    roman: str | None = None
    origin: str = "ai"
    engine_name: str = ""


def _prepare(ctx: ProjectContext, segment_id: str, language: str) -> tuple[list[str], dict, dict[str, Lock], dict[str, str], list[dict]]:
    line = ctx.lines[ctx.by_id[segment_id]]
    context = segment_context(ctx, segment_id)
    locks = collect_locks(ctx, line.segment.text)
    text, idiom_tokens, idioms = _apply_idioms(line.segment.text, language)
    text, lock_tokens = _apply_locks(text, locks)
    context["idioms"] = idioms
    context["locked_terms"] = [lock.source for lock in lock_tokens.values()]
    return split_sentences(text) or [text], context, lock_tokens, idiom_tokens, idioms


def _fallback_source(ctx: ProjectContext, segment_id: str, language: str) -> list[str]:
    """Idioms as plain meaning and no placeholders: the safest MT input."""
    text = ctx.lines[ctx.by_id[segment_id]].segment.text
    for rx, idiom in _IDIOM_RX:
        text = rx.sub(idiom.plain, text, count=1)
    return split_sentences(text) or [text]


def _post_edit(draft_text: str, source: str, language: str, context: dict) -> tuple[str, list[str], bool]:
    text = lang.normalize_target(draft_text, language)
    notes: list[str] = []
    text, n = lang.apply_register(text, language, context["register"])
    if n:
        notes.append(f"register → {context['register']} ({context['register_reason']})")
    text, n = lang.apply_speaker_gender(text, language, context["speaker"]["gender"])
    if n:
        notes.append(f"speaker gender → {context['speaker']['gender']}")
    text, n = lang.apply_listener_gender(text, language, context["listener"]["gender"])
    if n:
        notes.append("listener gender → female")
    text, punct_changed = lang.preserve_punctuation(source, text, language)
    return text, notes, punct_changed


def _refiner_prompt(source: str, draft: str, language: str, context: dict, locks: dict[str, Lock]) -> str:
    target = "Pakistani Urdu (Urdu script)" if language == "ur" else "Hindi (Devanagari)"
    lines = [f"Target language: {target}"]
    if context.get("scene_note"):
        lines.append(f"Scene: {context['scene_note']}")
    for item in context["previous"]:
        lines.append(f"Previous — {item['speaker']}: {item['text']}")
    speaker = context["speaker"]
    lines.append(
        f"Speaker: {speaker.get('name') or speaker.get('label') or 'Unknown'}; gender {speaker.get('gender')}; "
        f"style: {speaker.get('style') or 'not specified'}"
    )
    listener = context["listener"]
    if listener.get("name"):
        lines.append(f"Listener: {listener['name']}; gender {listener.get('gender')}")
    lines.append(f"Register: {context['register']} ({context['register_reason']}); tone: {context['tone']}")
    if locks:
        fixed = ", ".join(f"{lock.source} = {lock.targets.get(language, lock.source)}" for lock in locks.values())
        lines.append(f"Fixed spellings: {fixed}")
    if context.get("idioms"):
        lines.append("Idioms in the line: " + ", ".join(item["idiom"] for item in context["idioms"]))
    lines.append(f"English line: {source}")
    for item in context["following"]:
        lines.append(f"Next — {item['speaker']}: {item['text']}")
    lines.append(f"Machine draft: {draft}")
    return "\n".join(lines)


def translate_lines(
    db: Session,
    ctx: ProjectContext,
    segment_ids: list[str],
    language: str,
    check_active: ActiveCheck | None = None,
    use_memory: bool = True,
) -> list[Draft]:
    backend = engine.effective_backend()
    prepared: dict[str, tuple] = {}
    sentences: list[str] = []
    owners: list[str] = []
    drafts: list[Draft] = []

    for segment_id in segment_ids:
        line = ctx.lines[ctx.by_id[segment_id]]
        memory_key = (language, _normalise_source(line.segment.text), line.speaker.character_id)
        if use_memory and memory_key in ctx.memory and ctx.memory[memory_key][1] != segment_id:
            context = segment_context(ctx, segment_id)
            context["memory"] = True
            text = ctx.memory[memory_key][0]
            drafts.append(
                Draft(segment_id, language, text, text, [lang.flag("from_memory", "info", "Reused your earlier correction of an identical line.")], context, origin="memory", engine_name="memory")
            )
            continue
        parts, context, lock_tokens, idiom_tokens, idioms = _prepare(ctx, segment_id, language)
        prepared[segment_id] = (context, lock_tokens, idiom_tokens, len(parts))
        sentences.extend(parts)
        owners.extend([segment_id] * len(parts))

    outputs = engine.translate_texts(sentences, language, check_active=check_active) if sentences else []
    grouped: dict[str, list[str]] = {}
    for owner, output in zip(owners, outputs):
        grouped.setdefault(owner, []).append(output)

    retry_ids: list[str] = []
    for segment_id in segment_ids:
        if segment_id not in prepared:
            continue
        context, lock_tokens, idiom_tokens, _count = prepared[segment_id]
        raw = " ".join(grouped.get(segment_id, [])).strip()
        tokens = {token: lock.targets.get(language, lock.source) for token, lock in lock_tokens.items()}
        tokens.update(idiom_tokens)
        restored, missing = _restore(raw, tokens)
        if missing:
            retry_ids.append(segment_id)
            context["placeholder_fallback"] = missing
            continue
        drafts.append(Draft(segment_id, language, restored, raw, [], context, engine_name=backend))

    if retry_ids:
        if check_active:
            check_active()
        fallback_sentences: list[str] = []
        fallback_owners: list[str] = []
        for segment_id in retry_ids:
            parts = _fallback_source(ctx, segment_id, language)
            fallback_sentences.extend(parts)
            fallback_owners.extend([segment_id] * len(parts))
        fallback_out = engine.translate_texts(fallback_sentences, language, check_active=check_active)
        fallback_grouped: dict[str, list[str]] = {}
        for owner, output in zip(fallback_owners, fallback_out):
            fallback_grouped.setdefault(owner, []).append(output)
        for segment_id in retry_ids:
            context, lock_tokens = prepared[segment_id][0], prepared[segment_id][1]
            raw = " ".join(fallback_grouped.get(segment_id, [])).strip()
            text, missing_names = _repair_names(raw, lock_tokens, language)
            flags = []
            if missing_names:
                flags.append(lang.flag("name_check", "warning", "Check the spelling of: " + ", ".join(missing_names)))
            drafts.append(Draft(segment_id, language, text, raw, flags, context, engine_name=backend))

    order = {segment_id: index for index, segment_id in enumerate(segment_ids)}
    drafts.sort(key=lambda item: order[item.segment_id])

    for draft in drafts:
        if draft.origin == "memory":
            continue
        line = ctx.lines[ctx.by_id[draft.segment_id]]
        source = line.segment.text
        text, notes, punct_changed = _post_edit(draft.text, source, language, draft.context)
        draft.context["adjustments"] = notes
        if punct_changed:
            draft.context["punctuation_fixed"] = True
        draft.text = text
        lock_tokens = prepared.get(draft.segment_id, ({}, {}, {}, 0))[1]
        if settings.translation_refiner_url:
            if check_active:
                check_active()
            refined = engine.refine_with_llm(_refiner_prompt(source, text, language, draft.context, lock_tokens))
            if refined:
                refined_text = lang.normalize_target(refined["text"], language)
                refined_text, _ = lang.preserve_punctuation(source, refined_text, language)
                if not any(item["severity"] == "error" for item in lang.quality_flags(source, refined_text, language)):
                    draft.context["refined_from"] = text
                    draft.text = refined_text
                    draft.roman = refined.get("roman") if language == "ur" else None
                    draft.engine_name = f"{backend}+llm"
            else:
                draft.flags.append(lang.flag("refiner_unavailable", "info", "Context refiner did not respond; machine draft kept."))
        if draft.context.get("humor"):
            draft.flags.append(lang.flag("humor_review", "warning", "Joke or wordplay: check that the humour works in translation."))
        if draft.context.get("idioms"):
            draft.flags.append(
                lang.flag(
                    "idiom_adapted",
                    "info",
                    "Idiom adapted: " + "; ".join(f"“{item['idiom']}” → {item['rendering']}" for item in draft.context["idioms"]),
                )
            )
    return drafts


# ---------------------------------------------------------------------------
# Scoring, persistence and serialisation
# ---------------------------------------------------------------------------


def dubbing_fit(source: str, target: str, language: str, start_ms: int, end_ms: int) -> dict:
    duration_s = max(0.001, (end_ms - start_ms) / 1000)
    syllables = lang.target_syllables(target, language) if target.strip() else 0
    src_syllables = lang.english_syllables(source)
    estimate_s = syllables / max(0.1, settings.translation_comfortable_syllables_per_s)
    ratio = estimate_s / duration_s
    if not target.strip():
        status = "empty"
    elif ratio <= 1.0 or estimate_s - duration_s < 0.25:
        status = "ok"
    elif ratio <= 1.25:
        status = "tight"
    else:
        status = "long"
    return {
        "status": status,
        "duration_s": round(duration_s, 2),
        "estimated_s": round(estimate_s, 2),
        "ratio": round(ratio, 2),
        "target_syllables": syllables,
        "source_syllables": src_syllables,
        "length_vs_source": round(syllables / src_syllables, 2) if src_syllables else None,
    }


def _allowed_latin(ctx: ProjectContext | None) -> set[str]:
    if ctx is None:
        return set()
    words: set[str] = set()
    for character in ctx.characters.values():
        words.update(re.findall(r"[A-Za-z']+", character.name or ""))
    for term in ctx.glossary:
        for value in (term.source, term.ur or "", term.hi or ""):
            words.update(re.findall(r"[A-Za-z']+", value))
    return words


def score(row: SegmentTranslation, segment: TranscriptSegment, extra_flags: list[dict], ctx: ProjectContext | None) -> None:
    flags = lang.quality_flags(segment.text, row.text, row.language, _allowed_latin(ctx))
    keep = [item for item in extra_flags if item["code"] not in {flag["code"] for flag in flags}]
    if row.origin == "manual":
        keep = [item for item in keep if item["code"] in {"humor_review"}]
    fit = dubbing_fit(segment.text, row.text, row.language, segment.start_ms, segment.end_ms)
    if fit["status"] == "long":
        flags.append(
            lang.flag(
                "timing_long",
                "warning",
                f"Needs about {fit['estimated_s']}s of speech; the line lasts {fit['duration_s']}s. Consider a shorter phrasing.",
            )
        )
    elif fit["status"] == "tight":
        flags.append(lang.flag("timing_tight", "info", f"Tight fit: ~{fit['estimated_s']}s for a {fit['duration_s']}s line."))
    row.qc_flags = flags + keep
    row.fit_json = fit


def _push_history(row: SegmentTranslation, reason: str) -> None:
    if not (row.text or "").strip() and not (row.roman_text or "").strip():
        return
    history = list(row.history_json or [])
    history.append(
        {
            "text": row.text,
            "roman_text": row.roman_text,
            "origin": row.origin,
            "at": _utc().isoformat(),
            "reason": reason,
        }
    )
    row.history_json = history[-HISTORY_LIMIT:]


def roman_name_map(ctx: ProjectContext | None) -> dict[str, str]:
    mapping: dict[str, str] = {}
    if ctx is None:
        return mapping
    for profile in ctx.profiles.values():
        character = ctx.characters.get(profile.character_id)
        if profile.name_ur:
            mapping[profile.name_ur] = profile.name_roman or (character.name if character else profile.name_ur)
    for term in ctx.glossary:
        if term.ur:
            mapping[term.ur] = term.roman or term.source
    return mapping


def persist_draft(
    db: Session,
    video: Video,
    ctx: ProjectContext,
    draft: Draft,
    overwrite_manual: bool,
    job_id: str | None,
) -> SegmentTranslation:
    segment = ctx.lines[ctx.by_id[draft.segment_id]].segment
    row = db.scalar(
        select(SegmentTranslation).where(
            SegmentTranslation.segment_id == draft.segment_id, SegmentTranslation.language == draft.language
        )
    )
    if row is None:
        row = SegmentTranslation(
            project_id=video.project_id,
            video_id=video.id,
            segment_id=draft.segment_id,
            language=draft.language,
            text="",
            source_text=segment.text,
            history_json=[],
            revision=0,
        )
        db.add(row)
    roman = None
    if draft.language == "ur":
        roman = draft.roman or lang.romanize_urdu(draft.text, roman_name_map(ctx))
    row.ai_text = draft.text
    row.ai_roman_text = roman
    row.raw_mt = draft.raw_mt
    row.engine = draft.engine_name
    row.model = engine.model_id_for(engine.effective_backend()) if draft.origin != "memory" else "translation-memory"
    row.context_json = draft.context
    row.job_id = job_id
    row.error_message = None
    protected = row.origin == "manual" and not overwrite_manual
    if protected:
        # Keep the user's text; the fresh AI suggestion is available to compare.
        row.revision = (row.revision or 0) + 1
        score(row, segment, draft.flags, ctx)
        return row
    if row.text and row.text != draft.text:
        _push_history(row, "regenerated")
    row.text = draft.text
    row.origin = draft.origin
    row.status = "translated" if draft.text.strip() else "empty"
    row.source_text = segment.text
    if draft.language == "ur":
        row.roman_text = roman
        row.roman_origin = "ai" if draft.roman else "auto"
    row.revision = (row.revision or 0) + 1
    score(row, segment, draft.flags, ctx)
    return row


def serialize_translation(row: SegmentTranslation | None, segment: TranscriptSegment) -> dict | None:
    if row is None:
        return None
    flags = list(row.qc_flags or [])
    stale = (row.source_text or "").strip() != segment.text.strip()
    if stale:
        flags.insert(0, lang.flag("source_changed", "warning", "The English line changed after this was translated."))
    suggestion = bool(row.ai_text and row.origin == "manual" and row.ai_text.strip() != row.text.strip())
    return {
        "id": row.id,
        "language": row.language,
        "text": row.text,
        "roman_text": row.roman_text,
        "origin": row.origin,
        "roman_origin": row.roman_origin,
        "status": "stale" if stale else row.status,
        "ai_text": row.ai_text,
        "ai_roman_text": row.ai_roman_text,
        "raw_mt": row.raw_mt,
        "suggestion_available": suggestion,
        "source_text": row.source_text,
        "stale": stale,
        "engine": row.engine,
        "model": row.model,
        "qc_flags": flags,
        "fit": row.fit_json or {},
        "context": row.context_json or {},
        "history": row.history_json or [],
        "revision": row.revision,
        "error_message": row.error_message,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def serialize_line(line: DialogueLine, rows: dict[str, SegmentTranslation]) -> dict:
    segment = line.segment
    return {
        "segment_id": segment.id,
        "position": segment.position,
        "start_ms": segment.start_ms,
        "end_ms": segment.end_ms,
        "text": segment.text,
        "speaker_label": segment.speaker,
        "character_id": line.speaker.character_id,
        "character_name": line.speaker.name,
        "scene_index": line.scene_index,
        "scene_start": line.scene_start_id == segment.id,
        "translations": {
            language: serialize_translation(rows.get(language), segment) for language in LANGUAGES
        },
    }


def load_rows(db: Session, segment_ids: list[str]) -> dict[str, dict[str, SegmentTranslation]]:
    rows: dict[str, dict[str, SegmentTranslation]] = {}
    if not segment_ids:
        return rows
    for row in db.scalars(select(SegmentTranslation).where(SegmentTranslation.segment_id.in_(segment_ids))):
        rows.setdefault(row.segment_id, {})[row.language] = row
    return rows


def translate_segments(
    db: Session,
    video: Video,
    segment_ids: list[str] | None,
    languages: list[str],
    *,
    overwrite_manual: bool = False,
    only_missing: bool = False,
    job_id: str | None = None,
    skip_job_id: str | None = None,
    check_active: ActiveCheck | None = None,
    progress: Callable[[int, int, str], None] | None = None,
) -> dict:
    transcript = latest_transcript(db, video.id)
    if transcript is None or not transcript.segments:
        raise AppError(404, "TRANSCRIPT_NOT_FOUND", "Transcribe this video before translating it.")
    languages = [item for item in LANGUAGES if item in set(languages)]
    if not languages:
        raise AppError(422, "NO_TARGET_LANGUAGE", "Choose Urdu, Hindi or both.")
    ctx = build_context(db, video, transcript)
    wanted = [line.segment.id for line in ctx.lines] if not segment_ids else [
        segment_id for segment_id in segment_ids if segment_id in ctx.by_id
    ]
    if segment_ids and len(wanted) != len(set(segment_ids)):
        raise AppError(404, "SEGMENT_NOT_FOUND", "One or more dialogue segments do not belong to this transcript.")
    resolve_character_names(db, ctx, languages)
    db.commit()

    existing = load_rows(db, wanted)
    stats = {"translated": 0, "kept_manual": 0, "skipped": 0, "memory": 0, "flagged": 0}
    batch_size = max(1, settings.translation_batch_size)
    work: list[tuple[str, list[str]]] = []
    for language in languages:
        todo = []
        for segment_id in wanted:
            row = existing.get(segment_id, {}).get(language)
            if skip_job_id and row is not None and row.job_id == skip_job_id:
                stats["skipped"] += 1
                continue
            if only_missing and row is not None and row.text.strip() and row.source_text.strip() == ctx.lines[ctx.by_id[segment_id]].segment.text.strip():
                stats["skipped"] += 1
                continue
            todo.append(segment_id)
        for start in range(0, len(todo), batch_size):
            work.append((language, todo[start : start + batch_size]))

    total = len(work)
    for done, (language, batch) in enumerate(work):
        if check_active:
            check_active()
        if progress:
            progress(done, total, language)
        drafts = translate_lines(db, ctx, batch, language, check_active=check_active)
        for draft in drafts:
            before = existing.get(draft.segment_id, {}).get(language)
            was_manual = before is not None and before.origin == "manual" and not overwrite_manual
            row = persist_draft(db, video, ctx, draft, overwrite_manual, job_id)
            if was_manual:
                stats["kept_manual"] += 1
            elif draft.origin == "memory":
                stats["memory"] += 1
            else:
                stats["translated"] += 1
            if any(item["severity"] in {"error", "warning"} for item in row.qc_flags or []):
                stats["flagged"] += 1
        db.commit()  # persist each batch so long films make durable progress
    if progress:
        progress(total, total, languages[-1])
    return stats


def save_manual(
    db: Session,
    row: SegmentTranslation,
    segment: TranscriptSegment,
    text: str | None,
    roman_text: str | None,
    base_revision: int | None,
) -> SegmentTranslation:
    if base_revision is not None and base_revision != row.revision:
        raise AppError(
            409,
            "TRANSLATION_REVISION_CONFLICT",
            f"This translation changed since you loaded it (revision {row.revision}). Reload before saving.",
        )
    video = db.get(Video, row.video_id)
    transcript = latest_transcript(db, row.video_id)
    ctx = build_context(db, video, transcript) if video and transcript else None
    changed_text = text is not None and text.strip() != (row.text or "").strip()
    changed_roman = roman_text is not None and (roman_text or "").strip() != (row.roman_text or "").strip()
    if not changed_text and not changed_roman:
        return row
    _push_history(row, "manual edit")
    if changed_text:
        clean = lang.normalize_target(text or "", row.language)
        row.text = clean
        # Typing back exactly the AI suggestion (e.g. undo) restores AI status.
        row.origin = "ai" if row.ai_text and clean == lang.normalize_target(row.ai_text, row.language) else "manual"
        row.status = "edited" if row.origin == "manual" else ("translated" if clean else "empty")
        if not clean:
            row.status = "empty"
        row.source_text = segment.text
        if row.language == "ur" and not changed_roman and row.roman_origin != "manual":
            row.roman_text = lang.romanize_urdu(clean, roman_name_map(ctx))
            row.roman_origin = "auto"
    if changed_roman:
        row.roman_text = (roman_text or "").strip()
        row.roman_origin = "manual"
    row.revision = (row.revision or 0) + 1
    score(row, segment, [item for item in (row.qc_flags or []) if item["code"] in {"humor_review"}], ctx)
    db.commit()
    db.refresh(row)
    return row


def restore_version(db: Session, row: SegmentTranslation, segment: TranscriptSegment, target: str, index: int | None) -> SegmentTranslation:
    video = db.get(Video, row.video_id)
    transcript = latest_transcript(db, row.video_id)
    ctx = build_context(db, video, transcript) if video and transcript else None
    if target == "ai":
        if not row.ai_text:
            raise AppError(409, "NO_AI_SUGGESTION", "There is no AI suggestion to restore.")
        _push_history(row, "restored AI")
        row.text = row.ai_text
        row.roman_text = row.ai_roman_text if row.language == "ur" else None
        row.roman_origin = "auto" if row.language == "ur" else None
        row.origin = "ai"
        row.status = "translated"
    else:
        history = list(row.history_json or [])
        if index is None or index < 0 or index >= len(history):
            raise AppError(404, "HISTORY_NOT_FOUND", "That history entry does not exist.")
        entry = history[index]
        _push_history(row, "restored history")
        row.text = entry.get("text") or ""
        row.roman_text = entry.get("roman_text")
        row.origin = entry.get("origin") or "manual"
        row.status = "edited" if row.origin == "manual" else "translated"
    row.source_text = segment.text
    row.revision = (row.revision or 0) + 1
    score(row, segment, [], ctx)
    db.commit()
    db.refresh(row)
    return row


def romanize_row(db: Session, row: SegmentTranslation) -> SegmentTranslation:
    if row.language != "ur":
        raise AppError(422, "ROMAN_URDU_ONLY", "Roman transliteration is only available for Urdu.")
    video = db.get(Video, row.video_id)
    transcript = latest_transcript(db, row.video_id)
    ctx = build_context(db, video, transcript) if video and transcript else None
    _push_history(row, "regenerated roman")
    row.roman_text = lang.romanize_urdu(row.text, roman_name_map(ctx))
    row.roman_origin = "auto"
    row.revision = (row.revision or 0) + 1
    db.commit()
    db.refresh(row)
    return row


# ---------------------------------------------------------------------------
# Background job
# ---------------------------------------------------------------------------


def create_translation_job(db: Session, video: Video, options: dict) -> ProcessingJob:
    from .jobs import create_import_job

    active = db.scalar(
        select(ProcessingJob).where(
            ProcessingJob.video_id == video.id,
            ProcessingJob.type == "translation",
            ProcessingJob.status.in_(["queued", "running"]),
        )
    )
    if active:
        raise AppError(409, "TRANSLATION_JOB_ACTIVE", "A translation job is already running for this video.")
    return create_import_job(db, video.project_id, video.id, "translation", TRANSLATION_STEP_NAMES, options)


def run_translation_job(job_id: str, video_id: str, options: dict | None = None) -> None:
    from .jobs import JobCancelled, _job_status, _set_step

    db = SessionLocal()
    try:
        job = db.get(ProcessingJob, job_id)
        video = db.get(Video, video_id)
        if not job or not video:
            return
        opts = options or job.options_json or {}
        languages = [item for item in opts.get("languages") or list(LANGUAGES) if item in LANGUAGES]

        def check_active() -> None:
            status = _job_status(job_id)
            if status is None or status == "cancelled":
                raise JobCancelled("Job was cancelled or removed")

        # Claim atomically: a job cancelled while queued must stay cancelled.
        claim = db.execute(
            update(ProcessingJob)
            .where(ProcessingJob.id == job_id, ProcessingJob.status == "queued")
            .values(status="running")
        )
        db.commit()
        if claim.rowcount != 1:
            db.refresh(job)
            if job.status != "cancelled":
                return
            raise JobCancelled("Job was cancelled before it started")
        db.refresh(job)
        check_active()

        _set_step(db, job, "load_transcript", "running", "Reading dialogue", 3)
        transcript = latest_transcript(db, video.id)
        if transcript is None or not transcript.segments:
            raise AppError(404, "TRANSCRIPT_NOT_FOUND", "Transcribe this video before translating it.")
        _set_step(db, job, "load_transcript", "completed", f"{len(transcript.segments)} dialogue lines", 6)

        _set_step(db, job, "prepare_context", "running", "Characters, scenes, glossary", 8)
        check_active()
        ctx = build_context(db, video, transcript)
        scenes = (ctx.lines[-1].scene_index + 1) if ctx.lines else 0
        _set_step(db, job, "prepare_context", "completed", f"{scenes} scenes · {len(_named_characters(ctx))} named characters", 10)

        _set_step(db, job, "load_model", "running", f"Loading {engine.model_id_for(engine.effective_backend())}", 12)
        check_active()
        if engine.effective_backend() != "remote":
            engine.translate_texts(["Hello."], languages[0], check_active=check_active)  # warm + validate
        _set_step(db, job, "load_model", "completed", engine.engine_info()["label"], 18)

        _set_step(db, job, "translate", "running", "Translating", 20)

        def on_progress(done: int, total: int, language: str) -> None:
            percent = 20 + int(72 * (done / total)) if total else 92
            step = next((item for item in job.steps if item.name == "translate"), None)
            if step:
                step.detail = f"{'Urdu' if language == 'ur' else 'Hindi'} · batch {min(done + 1, total)}/{total}"
            job.progress = min(92, percent)
            job.current_step = "translate"
            db.commit()

        stats = translate_segments(
            db,
            video,
            opts.get("segment_ids") or None,
            languages,
            overwrite_manual=bool(opts.get("overwrite_manual")),
            only_missing=bool(opts.get("only_missing")),
            job_id=job_id,
            skip_job_id=opts.get("resume_from_job_id"),
            check_active=check_active,
            progress=on_progress,
        )
        _set_step(
            db,
            job,
            "translate",
            "completed",
            f"{stats['translated']} translated · {stats['kept_manual']} manual kept · {stats['memory']} from memory · {stats['skipped']} skipped",
            93,
        )
        _set_step(db, job, "quality_check", "completed", f"{stats['flagged']} lines flagged for review", 96)
        metadata = dict(video.metadata_json or {})
        metadata["translation"] = {**stats, "languages": languages, "engine": engine.effective_backend(), "updated_at": utcnow().isoformat()}
        video.metadata_json = metadata
        claim = db.execute(
            update(ProcessingJob)
            .where(ProcessingJob.id == job_id, ProcessingJob.status == "running")
            .values(status="completed", current_step="finalize", progress=100, error_code=None, error_message=None)
        )
        if claim.rowcount != 1:
            db.rollback()
            raise JobCancelled("Job was cancelled before finishing")
        db.commit()
        db.refresh(job)
        _set_step(db, job, "finalize", "completed", "Translations ready for review", 100)
    except Exception as error:  # noqa: BLE001 - job runner must record every failure
        from .jobs import JobCancelled as _Cancelled

        db.rollback()
        current = db.get(ProcessingJob, job_id)
        if current is None:
            return
        if isinstance(error, (_Cancelled, engine.TranslationCancelled)):
            if current.status != "cancelled":
                current.status = "cancelled"
                current.error_code = "CANCELLED"
                current.error_message = "Cancelled by user"
            detail, final = "Cancelled by user", "cancelled"
        elif current.status == "cancelled":
            detail, final = "Cancelled by user", "cancelled"
        else:
            code = error.code if isinstance(error, AppError) else (
                "TRANSLATION_ENGINE_UNAVAILABLE" if isinstance(error, engine.TranslationUnavailable) else "TRANSLATION_FAILED"
            )
            current.status = "failed"
            current.error_code = code
            current.error_message = str(error)[:1000]
            detail, final = str(error)[:500], "failed"
        for step in current.steps:
            if step.status in {"pending", "running"} and (final == "cancelled" or step.status == "running"):
                step.status = final
                step.detail = detail
                step.finished_at = utcnow()
        db.commit()
    finally:
        db.close()
