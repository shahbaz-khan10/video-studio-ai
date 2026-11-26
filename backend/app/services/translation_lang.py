"""Language-specific helpers for Urdu (Perso-Arabic) and Hindi (Devanagari).

Everything here is deterministic, rule-based post-editing applied on top of a
sentence-level MT model. Each rule is deliberately narrow (it only fires on
unambiguous grammatical patterns) and every change is reported back so the
editor can show what was adjusted and why.
"""

from __future__ import annotations

import re
import unicodedata

# ---------------------------------------------------------------------------
# Script normalisation
# ---------------------------------------------------------------------------

_URDU_NORMALISE = str.maketrans(
    {
        "ي": "ی",  # Arabic yeh -> Farsi/Urdu yeh
        "ى": "ی",  # alef maksura -> yeh
        "ك": "ک",  # Arabic kaf -> keheh
        "ه": "ہ",  # Arabic heh -> heh goal (medial/final forms)
    }
)

URDU_RANGE = re.compile(r"[ء-غف-ٰٟ-ۓەݐ-ݿﭐ-﷿ﹰ-﻿]")
DEVANAGARI_RANGE = re.compile(r"[ऀ-ॣ०-ॿ]")
LATIN_WORD = re.compile(r"[A-Za-z][A-Za-z'\-]+")


def normalize_target(text: str, language: str) -> str:
    text = unicodedata.normalize("NFC", text or "")
    if language == "ur":
        text = text.translate(_URDU_NORMALISE)
        # heh-doachashmee must survive; only the bare Arabic heh was remapped.
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\s+([,.!?؟۔،।])", r"\1", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Punctuation preservation
# ---------------------------------------------------------------------------

_TERMINALS = {
    "ur": {"?": "؟", ".": "۔", "!": "!", "…": "…"},
    "hi": {"?": "?", ".": "।", "!": "!", "…": "…"},
}
_TARGET_TERMINAL_CHARS = set("?؟.۔।!…")


def _source_terminal(source: str) -> str | None:
    stripped = source.rstrip().rstrip("\"'”’)")
    if stripped.endswith("...") or stripped.endswith("…"):
        return "…"
    if stripped.endswith("?!") or stripped.endswith("!?"):
        return "?"
    if stripped and stripped[-1] in "?!.":
        return stripped[-1]
    return None


def _internal_full_stops(text: str, language: str) -> str:
    """Sentence-internal '.' → '۔' (Urdu) / '।' (Hindi); digits are left alone."""
    stop = "۔" if language == "ur" else "।"
    script = r"[ء-غف-ٰٟ-ۓە]" if language == "ur" else r"[ऀ-ॣ०-ॿ]"
    return re.sub(rf"(?<={script})\.(?=\s)", stop, text)


def preserve_punctuation(source: str, target: str, language: str) -> tuple[str, bool]:
    """Make the target's sentence-final mark agree with the source's.

    Returns (text, changed). Commas become the Urdu comma in Urdu script.
    """
    if not target:
        return target, False
    original = target
    if language == "ur":
        target = target.replace(",", "،").replace("?", "؟")
    elif language == "hi":
        target = target.replace("؟", "?").replace("۔", "।")
    wanted = _source_terminal(source)
    body = target.rstrip()
    trailing_quote = ""
    while body and body[-1] in "\"'”’)":
        trailing_quote = body[-1] + trailing_quote
        body = body[:-1]
    current = ""
    while body and body[-1] in _TARGET_TERMINAL_CHARS:
        current = body[-1] + current
        body = body[:-1]
    if wanted is None:
        body = _internal_full_stops(body, language)
        # Source had no terminal (a fragment); don't invent one.
        target = (body + current + trailing_quote) if current not in {".", "۔", "।"} else body + trailing_quote
        if _source_terminal(source) is None and current in {".", "۔", "।"}:
            target = body + trailing_quote
    else:
        mark = _TERMINALS[language][wanted]
        body = _internal_full_stops(body, language)
        if wanted == "?" and source.rstrip().endswith(("?!", "!?")):
            mark = mark + "!"
        target = body.rstrip() + mark + trailing_quote
    return target, target != original


# ---------------------------------------------------------------------------
# Register: formal (aap) / informal (tum) / intimate (tu)
# ---------------------------------------------------------------------------

REGISTERS = ("formal", "neutral", "informal", "intimate")

_HI_STOP_EN = {"में", "हमें", "उन्हें", "तुम्हें", "इन्हें", "जिन्हें", "किन्हें", "मुझें", "हैं", "नहीं", "कहीं", "यहीं", "वहीं"}
_UR_STOP_YN = {"میں", "ہیں", "نہیں", "کہیں", "انہیں", "تمہیں", "ہمیں", "یہیں", "وہیں", "جنہیں", "کنہیں", "انھیں", "ہوں"}

_HI_FORMAL_TO_INFORMAL = [
    ("आपको", "तुम्हें"),
    ("आपसे", "तुमसे"),
    ("आपका", "तुम्हारा"),
    ("आपकी", "तुम्हारी"),
    ("आपके", "तुम्हारे"),
    ("आपने", "तुमने"),
    ("आपमें", "तुममें"),
    ("आपपर", "तुमपर"),
    ("आप", "तुम"),
]
_HI_INFORMAL_TO_INTIMATE = [
    ("तुम्हें", "तुझे"),
    ("तुमसे", "तुझसे"),
    ("तुम्हारा", "तेरा"),
    ("तुम्हारी", "तेरी"),
    ("तुम्हारे", "तेरे"),
    ("तुमने", "तूने"),
    ("तुममें", "तुझमें"),
    ("तुमपर", "तुझपर"),
    ("तुम", "तू"),
]
_HI_IMPERATIVE_POLITE = {
    "कीजिए": "करो", "कीजिये": "करो", "करिए": "करो", "करिये": "करो",
    "दीजिए": "दो", "दीजिये": "दो", "लीजिए": "लो", "लीजिये": "लो",
    "पीजिए": "पियो", "पीजिये": "पियो", "जाइए": "जाओ", "जाइये": "जाओ",
    "आइए": "आओ", "आइये": "आओ",
}
_HI_INFORMAL_IMPERATIVE_TO_POLITE = {
    "करो": "कीजिए", "दो": "दीजिए", "लो": "लीजिए", "जाओ": "जाइए", "आओ": "आइए",
    "बैठो": "बैठिए", "सुनो": "सुनिए", "देखो": "देखिए", "रुको": "रुकिए", "चलो": "चलिए",
    "बताओ": "बताइए", "कहो": "कहिए", "निकलो": "निकलिए", "सोचो": "सोचिए", "खाओ": "खाइए",
    "पियो": "पीजिए", "छोड़ो": "छोड़िए", "समझो": "समझिए", "मानो": "मानिए", "भागो": "भागिए",
}
_HI_INTIMATE_IMPERATIVE = {
    "करो": "कर", "दो": "दे", "लो": "ले", "जाओ": "जा", "आओ": "आ", "बैठो": "बैठ", "सुनो": "सुन",
    "देखो": "देख", "रुको": "रुक", "चलो": "चल", "बताओ": "बता", "कहो": "कह", "निकलो": "निकल",
    "सोचो": "सोच", "खाओ": "खा", "छोड़ो": "छोड़", "समझो": "समझ", "भागो": "भाग",
}

_UR_FORMAL_TO_INFORMAL = [
    ("آپ کو", "تمہیں"),
    ("آپ سے", "تم سے"),
    ("آپ کا", "تمہارا"),
    ("آپ کی", "تمہاری"),
    ("آپ کے", "تمہارے"),
    ("آپ نے", "تم نے"),
    ("آپ میں", "تم میں"),
    ("آپ پر", "تم پر"),
    ("آپ", "تم"),
]
_UR_INFORMAL_TO_INTIMATE = [
    ("تمہیں", "تجھے"),
    ("تم سے", "تجھ سے"),
    ("تمہارا", "تیرا"),
    ("تمہاری", "تیری"),
    ("تمہارے", "تیرے"),
    ("تم نے", "تو نے"),
    ("تم میں", "تجھ میں"),
    ("تم پر", "تجھ پر"),
    ("تم", "تو"),
]
_UR_IMPERATIVE_POLITE = {
    "کیجیے": "کرو", "کیجئے": "کرو", "کیجیئے": "کرو", "کریے": "کرو",
    "دیجیے": "دو", "دیجئے": "دو", "لیجیے": "لو", "لیجئے": "لو",
    "پیجیے": "پیو", "جائیے": "جاؤ", "آئیے": "آؤ", "بیٹھیے": "بیٹھو",
    "سنیے": "سنو", "دیکھیے": "دیکھو", "رکیے": "رکو", "چلیے": "چلو",
    "بتائیے": "بتاؤ", "کہیے": "کہو", "نکلیے": "نکلو", "سوچیے": "سوچو",
}
_UR_INFORMAL_IMPERATIVE_TO_POLITE = {
    "کرو": "کیجیے", "دو": "دیجیے", "لو": "لیجیے", "جاؤ": "جائیے", "آؤ": "آئیے",
    "بیٹھو": "بیٹھیے", "سنو": "سنیے", "دیکھو": "دیکھیے", "رکو": "رکیے", "چلو": "چلیے",
    "بتاؤ": "بتائیے", "کہو": "کہیے", "نکلو": "نکلیے", "سوچو": "سوچیے", "کھاؤ": "کھائیے",
    "چھوڑو": "چھوڑیے", "سمجھو": "سمجھیے", "مانو": "مانیے", "بھاگو": "بھاگیے",
}
_UR_INTIMATE_IMPERATIVE = {
    "کرو": "کر", "دو": "دے", "لو": "لے", "جاؤ": "جا", "آؤ": "آ", "بیٹھو": "بیٹھ", "سنو": "سن",
    "دیکھو": "دیکھ", "رکو": "رک", "چلو": "چل", "بتاؤ": "بتا", "کہو": "کہہ", "نکلو": "نکل",
    "سوچو": "سوچ", "کھاؤ": "کھا", "چھوڑو": "چھوڑ", "سمجھو": "سمجھ", "بھاگو": "بھاگ",
}

_WORD_BOUNDARY_HI = r"(?<![ऀ-ॣ०-ॿ])"
_WORD_END_HI = r"(?![ऀ-ॣ०-ॿ])"
_WORD_BOUNDARY_UR = r"(?<![ء-غف-ٰٟ-ۓە])"
_WORD_END_UR = r"(?![ء-غف-ٰٟ-ۓە])"


def _sub_word(text: str, old: str, new: str, language: str) -> tuple[str, int]:
    if language == "hi":
        pattern = _WORD_BOUNDARY_HI + re.escape(old) + _WORD_END_HI
    else:
        pattern = _WORD_BOUNDARY_UR + re.escape(old) + _WORD_END_UR
    return re.subn(pattern, new, text)


def _split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?؟۔।])\s+", text)
    return [part for part in parts if part]


def detect_register(text: str, language: str) -> str | None:
    if language == "hi":
        if re.search(_WORD_BOUNDARY_HI + r"(तू|तुझे|तुझसे|तेरा|तेरी|तेरे|तूने)" + _WORD_END_HI, text):
            return "intimate"
        if re.search(_WORD_BOUNDARY_HI + r"(तुम|तुम्हें|तुमसे|तुम्हारा|तुम्हारी|तुम्हारे|तुमने)" + _WORD_END_HI, text):
            return "informal"
        if re.search(_WORD_BOUNDARY_HI + r"(आप|आपको|आपसे|आपका|आपकी|आपके|आपने)" + _WORD_END_HI, text):
            return "formal"
    else:
        if re.search(_WORD_BOUNDARY_UR + r"(تجھے|تجھ|تیرا|تیری|تیرے)" + _WORD_END_UR, text):
            return "intimate"
        if re.search(_WORD_BOUNDARY_UR + r"(تم|تمہیں|تمہارا|تمہاری|تمہارے)" + _WORD_END_UR, text):
            return "informal"
        if re.search(_WORD_BOUNDARY_UR + r"آپ" + _WORD_END_UR, text):
            return "formal"
    return None


def _last_word_swap(sentence: str, mapping: dict[str, str], language: str) -> tuple[str, bool]:
    """Swap the sentence-final verb (the imperative slot) using ``mapping``."""
    letters = r"[ऀ-ॣ०-ॿ]+" if language == "hi" else r"[ء-غف-ٰٟ-ۓە]+"
    match = re.search(rf"({letters})(\s*[!.?؟۔।…]*\s*[\"'”’)]*\s*)$", sentence)
    if not match:
        return sentence, False
    word = match.group(1)
    if word in mapping:
        return sentence[: match.start(1)] + mapping[word] + match.group(2), True
    return sentence, False


def _hi_formal_to_informal(sentence: str) -> tuple[str, list[str]]:
    notes: list[str] = []
    had_aap = bool(re.search(_WORD_BOUNDARY_HI + r"आप", sentence))
    for old, new in _HI_FORMAL_TO_INFORMAL:
        sentence, count = _sub_word(sentence, old, new, "hi")
        if count:
            notes.append(f"{old}→{new}")
    if had_aap:
        # Verb agreement with तुम: हैं → हो (only the auxiliary closing the clause).
        sentence, count = re.subn(r"(?<![ऀ-ॣ०-ॿ])हैं(?=\s*[?!.।…]*\s*$)", "हो", sentence)
        if count:
            notes.append("हैं→हो")
        for old, new in _HI_IMPERATIVE_POLITE.items():
            sentence, count = _sub_word(sentence, old, new, "hi")
            if count:
                notes.append(f"{old}→{new}")
        # Polite subjunctive at the end: करें → करो (stoplist protects postpositions).
        match = re.search(r"([ऀ-ॣ०-ॿ]+)ें(\s*[?!.।…]*\s*)$", sentence)
        if match and (match.group(1) + "ें") not in _HI_STOP_EN and len(match.group(1)) >= 2:
            sentence = sentence[: match.start()] + match.group(1) + "ो" + match.group(2)
            notes.append("-ें→-ो")
    else:
        # Bare polite imperative with no pronoun ("बैठिए!").
        for old, new in _HI_IMPERATIVE_POLITE.items():
            sentence, count = _sub_word(sentence, old, new, "hi")
            if count:
                notes.append(f"{old}→{new}")
    return sentence, notes


def _hi_informal_to_intimate(sentence: str) -> tuple[str, list[str]]:
    notes: list[str] = []
    had_tum = bool(re.search(_WORD_BOUNDARY_HI + r"तुम" + _WORD_END_HI, sentence))
    for old, new in _HI_INFORMAL_TO_INTIMATE:
        sentence, count = _sub_word(sentence, old, new, "hi")
        if count:
            notes.append(f"{old}→{new}")
    if had_tum:
        sentence, count = re.subn(r"(?<![ऀ-ॣ०-ॿ])हो(?=\s*[?!.।…]*\s*$)", "है", sentence)
        if count:
            notes.append("हो→है")
            sentence = re.sub(r"रहे(?=\s+है)", "रहा", sentence)
    sentence, swapped = _last_word_swap(sentence, _HI_INTIMATE_IMPERATIVE, "hi")
    if swapped:
        notes.append("imperative→tu form")
    return sentence, notes


def _hi_informal_to_formal(sentence: str) -> tuple[str, list[str]]:
    notes: list[str] = []
    had_tum = bool(re.search(_WORD_BOUNDARY_HI + r"(तुम|तू)" + _WORD_END_HI, sentence))
    reverse = [(new, old) for old, new in _HI_INFORMAL_TO_INTIMATE] + [(new, old) for old, new in _HI_FORMAL_TO_INFORMAL]
    for old, new in reverse:
        sentence, count = _sub_word(sentence, old, new, "hi")
        if count:
            notes.append(f"{old}→{new}")
    if had_tum:
        sentence, count = re.subn(r"(?<![ऀ-ॣ०-ॿ])(हो|है)(?=\s*[?!.।…]*\s*$)", "हैं", sentence)
        if count:
            notes.append("हो→हैं")
            sentence = re.sub(r"रहा(?=\s+हैं)", "रहे", sentence)
    sentence, swapped = _last_word_swap(sentence, _HI_INFORMAL_IMPERATIVE_TO_POLITE, "hi")
    if swapped:
        notes.append("imperative→polite")
    return sentence, notes


def _ur_formal_to_informal(sentence: str) -> tuple[str, list[str]]:
    notes: list[str] = []
    had_aap = bool(re.search(_WORD_BOUNDARY_UR + r"آپ" + _WORD_END_UR, sentence))
    for old, new in _UR_FORMAL_TO_INFORMAL:
        sentence, count = _sub_word(sentence, old, new, "ur")
        if count:
            notes.append(f"{old}→{new}")
    if had_aap:
        sentence, count = re.subn(r"(?<![ء-غف-ٰٟ-ۓە])ہیں(?=\s*[؟?!.۔…]*\s*$)", "ہو", sentence)
        if count:
            notes.append("ہیں→ہو")
        match = re.search(r"([ء-غف-ٰٟ-ۓە]+)یں(\s*[؟?!.۔…]*\s*)$", sentence)
        if match and (match.group(1) + "یں") not in _UR_STOP_YN and len(match.group(1)) >= 2:
            sentence = sentence[: match.start()] + match.group(1) + "و" + match.group(2)
            notes.append("-یں→-و")
    for old, new in _UR_IMPERATIVE_POLITE.items():
        sentence, count = _sub_word(sentence, old, new, "ur")
        if count:
            notes.append(f"{old}→{new}")
    return sentence, notes


def _ur_informal_to_intimate(sentence: str) -> tuple[str, list[str]]:
    notes: list[str] = []
    had_tum = bool(re.search(_WORD_BOUNDARY_UR + r"تم" + _WORD_END_UR, sentence))
    for old, new in _UR_INFORMAL_TO_INTIMATE:
        sentence, count = _sub_word(sentence, old, new, "ur")
        if count:
            notes.append(f"{old}→{new}")
    if had_tum:
        sentence, count = re.subn(r"(?<![ء-غف-ٰٟ-ۓە])ہو(?=\s*[؟?!.۔…]*\s*$)", "ہے", sentence)
        if count:
            notes.append("ہو→ہے")
            sentence = re.sub(r"رہے(?=\s+ہے)", "رہا", sentence)
    sentence, swapped = _last_word_swap(sentence, _UR_INTIMATE_IMPERATIVE, "ur")
    if swapped:
        notes.append("imperative→tu form")
    return sentence, notes


def _ur_informal_to_formal(sentence: str) -> tuple[str, list[str]]:
    notes: list[str] = []
    had_tum = bool(re.search(_WORD_BOUNDARY_UR + r"(تم|تجھ|تجھے)" + _WORD_END_UR, sentence))
    reverse = [(new, old) for old, new in _UR_INFORMAL_TO_INTIMATE if new != "تو"] + [
        (new, old) for old, new in _UR_FORMAL_TO_INFORMAL
    ]
    for old, new in reverse:
        sentence, count = _sub_word(sentence, old, new, "ur")
        if count:
            notes.append(f"{old}→{new}")
    if had_tum:
        sentence, count = re.subn(r"(?<![ء-غف-ٰٟ-ۓە])(ہو|ہے)(?=\s*[؟?!.۔…]*\s*$)", "ہیں", sentence)
        if count:
            notes.append("ہو→ہیں")
            sentence = re.sub(r"رہا(?=\s+ہیں)", "رہے", sentence)
    sentence, swapped = _last_word_swap(sentence, _UR_INFORMAL_IMPERATIVE_TO_POLITE, "ur")
    if swapped:
        notes.append("imperative→polite")
    return sentence, notes


def apply_register(text: str, language: str, register: str) -> tuple[str, list[str]]:
    """Rewrite second-person address to the requested register.

    ``neutral``/``auto`` leaves the model output alone. Returns (text, notes).
    """
    if register not in {"formal", "informal", "intimate"} or not text:
        return text, []
    out: list[str] = []
    notes: list[str] = []
    for sentence in _split_sentences(text):
        current = detect_register(sentence, language)
        if current is None:
            # No pronoun: only adjust a bare imperative (e.g. "Sit down!").
            if language == "hi":
                if register in {"informal", "intimate"}:
                    sentence, n = _hi_formal_to_informal(sentence)
                    notes += n
                    if register == "intimate":
                        sentence, n = _hi_informal_to_intimate(sentence)
                        notes += n
                elif register == "formal":
                    sentence, swapped = _last_word_swap(sentence, _HI_INFORMAL_IMPERATIVE_TO_POLITE, "hi")
                    if swapped:
                        notes.append("imperative→polite")
            else:
                if register in {"informal", "intimate"}:
                    sentence, n = _ur_formal_to_informal(sentence)
                    notes += n
                    if register == "intimate":
                        sentence, n = _ur_informal_to_intimate(sentence)
                        notes += n
                elif register == "formal":
                    sentence, swapped = _last_word_swap(sentence, _UR_INFORMAL_IMPERATIVE_TO_POLITE, "ur")
                    if swapped:
                        notes.append("imperative→polite")
            out.append(sentence)
            continue
        if current == register:
            out.append(sentence)
            continue
        if language == "hi":
            if register == "formal":
                sentence, n = _hi_informal_to_formal(sentence)
                notes += n
            else:
                if current == "formal":
                    sentence, n = _hi_formal_to_informal(sentence)
                    notes += n
                if register == "intimate":
                    sentence, n = _hi_informal_to_intimate(sentence)
                    notes += n
                elif current == "intimate":
                    # intimate → informal: go through formal first.
                    sentence, n = _hi_informal_to_formal(sentence)
                    notes += n
                    sentence, n = _hi_formal_to_informal(sentence)
                    notes += n
        else:
            if register == "formal":
                sentence, n = _ur_informal_to_formal(sentence)
                notes += n
            else:
                if current == "formal":
                    sentence, n = _ur_formal_to_informal(sentence)
                    notes += n
                if register == "intimate":
                    sentence, n = _ur_informal_to_intimate(sentence)
                    notes += n
                elif current == "intimate":
                    sentence, n = _ur_informal_to_formal(sentence)
                    notes += n
                    sentence, n = _ur_formal_to_informal(sentence)
                    notes += n
        out.append(sentence)
    return " ".join(out), notes


# ---------------------------------------------------------------------------
# Gender agreement (speaker = first person, listener = second person)
# ---------------------------------------------------------------------------

_HI_FIRST_PERSON_FEM = [
    (r"रहा(\s+)(हूँ|हूं)", r"रही\1\2"),
    (r"([ऀ-ॣ०-ॿ]+)ता(\s+)(हूँ|हूं)", r"\1ती\2\3"),
    (r"गया(\s+)(हूँ|हूं)", r"गई\1\2"),
    (r"चुका(\s+)(हूँ|हूं)", r"चुकी\1\2"),
    (r"हुआ(\s+)(हूँ|हूं)", r"हुई\1\2"),
    (r"([ऀ-ॣ०-ॿ]+)(ूँगा|ूंगा)", lambda m: m.group(1) + m.group(2)[:-1] + "ी"),
    (r"([ऀ-ॣ०-ॿ]+)(ऊँगा|ऊंगा)", lambda m: m.group(1) + m.group(2)[:-1] + "ी"),
]
_HI_FIRST_PERSON_PAST_FEM = [
    (r"रहा(\s+)था", r"रही\1थी"),
    (r"गया(\s+)था", r"गई\1थी"),
    (r"([ऀ-ॣ०-ॿ]+)ता(\s+)था", r"\1ती\2थी"),
    (r"(?<![ऀ-ॣ०-ॿ])था(?![ऀ-ॣ०-ॿ])", "थी"),
]
_HI_SECOND_PERSON_FEM_TUM = [
    (r"([ऀ-ॣ०-ॿ]+)े(\s+)हुए(\s+)हो", r"\1ी\2हुई\3हो"),
    (r"रहे(\s+)हो", r"रही\1हो"),
    (r"गए(\s+)हो", r"गई\1हो"),
    (r"([ऀ-ॣ०-ॿ]+)ते(\s+)हो", r"\1ती\2हो"),
    (r"([ऀ-ॣ०-ॿ]+)ोगे(?![ऀ-ॣ०-ॿ])", r"\1ोगी"),
]
_HI_SECOND_PERSON_FEM_AAP = [
    (r"([ऀ-ॣ०-ॿ]+)े(\s+)हुए(\s+)हैं", r"\1ी\2हुई\3हैं"),
    (r"रहे(\s+)हैं", r"रही\1हैं"),
    (r"गए(\s+)हैं", r"गई\1हैं"),
    (r"([ऀ-ॣ०-ॿ]+)ेंगे(?![ऀ-ॣ०-ॿ])", r"\1ेंगी"),
]
_HI_SECOND_PERSON_FEM_TU = [
    (r"रहा(\s+)है", r"रही\1है"),
    (r"गया(\s+)है", r"गई\1है"),
    (r"([ऀ-ॣ०-ॿ]+)ेगा(?![ऀ-ॣ०-ॿ])", r"\1ेगी"),
]

_UR_FIRST_PERSON_FEM = [
    (r"رہا(\s+)ہوں", r"رہی\1ہوں"),
    (r"([ء-غف-ٰٟ-ۓە]+)تا(\s+)ہوں", r"\1تی\2ہوں"),
    (r"گیا(\s+)ہوں", r"گئی\1ہوں"),
    (r"چکا(\s+)ہوں", r"چکی\1ہوں"),
    (r"ہوا(\s+)ہوں", r"ہوئی\1ہوں"),
    (r"(وں|ؤں)(\s+)گا(?![ء-غف-ٰٟ-ۓە])", r"\1\2گی"),
]
_UR_FIRST_PERSON_PAST_FEM = [
    (r"رہا(\s+)تھا", r"رہی\1تھی"),
    (r"گیا(\s+)تھا", r"گئی\1تھی"),
    (r"([ء-غف-ٰٟ-ۓە]+)تا(\s+)تھا", r"\1تی\2تھی"),
    (r"(?<![ء-غف-ٰٟ-ۓە])تھا(?![ء-غف-ٰٟ-ۓە])", "تھی"),
]
_UR_SECOND_PERSON_FEM_TUM = [
    (r"([ء-غف-ٰٟ-ۓە]+)ے(\s+)ہوئے(\s+)ہو(?![ء-غف-ٰٟ-ۓە])", r"\1ی\2ہوئی\3ہو"),
    (r"رہے(\s+)ہو", r"رہی\1ہو"),
    (r"گئے(\s+)ہو", r"گئی\1ہو"),
    (r"([ء-غف-ٰٟ-ۓە]+)تے(\s+)ہو(?![ء-غف-ٰٟ-ۓە])", r"\1تی\2ہو"),
    (r"(و|ؤ)(\s+)گے(?![ء-غف-ٰٟ-ۓە])", r"\1\2گی"),
]
_UR_SECOND_PERSON_FEM_AAP = [
    (r"([ء-غف-ٰٟ-ۓە]+)ے(\s+)ہوئے(\s+)ہیں", r"\1ی\2ہوئی\3ہیں"),
    (r"رہے(\s+)ہیں", r"رہی\1ہیں"),
    (r"گئے(\s+)ہیں", r"گئی\1ہیں"),
    (r"یں(\s+)گے(?![ء-غف-ٰٟ-ۓە])", r"یں\1گی"),
]
_UR_SECOND_PERSON_FEM_TU = [
    (r"رہا(\s+)ہے", r"رہی\1ہے"),
    (r"گیا(\s+)ہے", r"گئی\1ہے"),
    (r"ے(\s+)گا(?![ء-غف-ٰٟ-ۓە])", r"ے\1گی"),
]


def _apply_rules(sentence: str, rules: list) -> tuple[str, int]:
    total = 0
    for pattern, replacement in rules:
        sentence, count = re.subn(pattern, replacement, sentence)
        total += count
    return sentence, total


def _invert(rules: list) -> list:
    """Masculine variants for fixing a female-default output for a male speaker."""
    inverted = []
    for pattern, replacement in rules:
        if callable(replacement):
            continue
        pattern_f = (
            pattern.replace("रहा", "रही").replace("गया", "गई").replace("चुका", "चुकी").replace("हुआ", "हुई")
            .replace("ता(", "ती(").replace("था", "थी")
            .replace("رہا", "رہی").replace("گیا", "گئی").replace("چکا", "چکی").replace("ہوا", "ہوئی")
            .replace("تا(", "تی(").replace("تھا", "تھی").replace("گا(", "گی(")
            .replace("रहे", "रही").replace("गए", "गई").replace("ते(", "ती(").replace("ोगे", "ोगी").replace("ेंगे", "ेंगी").replace("ेगा", "ेगी")
            .replace("رہے", "رہی").replace("گئے", "گئی").replace("تے(", "تی(").replace("گے(", "گی(")
        )
        replacement_m = (
            replacement.replace("रही", "रहा").replace("गई", "गया").replace("चुकी", "चुका").replace("हुई", "हुआ")
            .replace("ती", "ता").replace("थी", "था")
            .replace("رہی", "رہا").replace("گئی", "گیا").replace("چکی", "چکا").replace("ہوئی", "ہوا")
            .replace("تی", "تا").replace("تھی", "تھا").replace("گی", "گا")
            .replace("ोगी", "ोगे").replace("ेंगी", "ेंगे").replace("ेगी", "ेगा")
        )
        if pattern_f != pattern:
            inverted.append((pattern_f, replacement_m))
    return inverted


def apply_speaker_gender(text: str, language: str, gender: str) -> tuple[str, list[str]]:
    """Fix first-person verb agreement for the speaker's gender."""
    if gender not in {"male", "female"} or not text:
        return text, []
    notes: list[str] = []
    out = []
    for sentence in _split_sentences(text):
        if language == "hi":
            first = bool(re.search(r"(हूँ|हूं)", sentence)) or sentence.lstrip().startswith("मैं")
            if gender == "female":
                sentence, count = _apply_rules(sentence, _HI_FIRST_PERSON_FEM)
                if sentence.lstrip().startswith("मैं") and not re.search(r"(?<![ऀ-ॣ०-ॿ])(वह|वो|वे|यह|ये|हम)(?![ऀ-ॣ०-ॿ])", sentence):
                    sentence, past = _apply_rules(sentence, _HI_FIRST_PERSON_PAST_FEM)
                    count += past
            else:
                sentence, count = _apply_rules(sentence, _invert(_HI_FIRST_PERSON_FEM)) if first else (sentence, 0)
                sentence = re.sub(r"([ऀ-ॣ०-ॿ]+)(ूँगी|ूंगी)", lambda m: m.group(1) + m.group(2)[:-1] + "ा", sentence)
        else:
            if gender == "female":
                sentence, count = _apply_rules(sentence, _UR_FIRST_PERSON_FEM)
                if sentence.lstrip().startswith("میں ") and not re.search(r"(?<![ء-غف-ٰٟ-ۓە])(وہ|یہ|ہم)(?![ء-غف-ٰٟ-ۓە])", sentence):
                    sentence, past = _apply_rules(sentence, _UR_FIRST_PERSON_PAST_FEM)
                    count += past
            else:
                sentence, count = _apply_rules(sentence, _invert(_UR_FIRST_PERSON_FEM))
        if count:
            notes.append(f"speaker {gender} agreement")
        out.append(sentence)
    return " ".join(out), notes


def apply_listener_gender(text: str, language: str, gender: str) -> tuple[str, list[str]]:
    """Fix second-person verb agreement when the addressee's gender is known."""
    if gender != "female" or not text:
        return text, []
    notes: list[str] = []
    out = []
    for sentence in _split_sentences(text):
        register = detect_register(sentence, language)
        count = 0
        if language == "hi":
            if register == "informal":
                sentence, count = _apply_rules(sentence, _HI_SECOND_PERSON_FEM_TUM)
            elif register == "formal":
                sentence, count = _apply_rules(sentence, _HI_SECOND_PERSON_FEM_AAP)
            elif register == "intimate":
                sentence, count = _apply_rules(sentence, _HI_SECOND_PERSON_FEM_TU)
        else:
            if register == "informal":
                sentence, count = _apply_rules(sentence, _UR_SECOND_PERSON_FEM_TUM)
            elif register == "formal":
                sentence, count = _apply_rules(sentence, _UR_SECOND_PERSON_FEM_AAP)
            elif register == "intimate":
                sentence, count = _apply_rules(sentence, _UR_SECOND_PERSON_FEM_TU)
        if count:
            notes.append("listener female agreement")
        out.append(sentence)
    return " ".join(out), notes


# ---------------------------------------------------------------------------
# Roman Urdu transliteration (lexicon first, rules as fallback)
# ---------------------------------------------------------------------------

ROMAN_LEXICON: dict[str, str] = {
    # pronouns
    "میں": "main", "مجھے": "mujhe", "مجھ": "mujh", "میرا": "mera", "میری": "meri", "میرے": "mere",
    "ہم": "hum", "ہمیں": "humein", "ہمارا": "hamara", "ہماری": "hamari", "ہمارے": "hamare",
    "آپ": "aap", "تم": "tum", "تمہیں": "tumhein", "تمہارا": "tumhara", "تمہاری": "tumhari", "تمہارے": "tumhare",
    "تو": "tu", "تجھے": "tujhe", "تجھ": "tujh", "تیرا": "tera", "تیری": "teri", "تیرے": "tere",
    "وہ": "woh", "یہ": "yeh", "اس": "is", "اسے": "use", "انہیں": "unhein", "ان": "un", "اسی": "isi",
    "اپنا": "apna", "اپنی": "apni", "اپنے": "apne", "کون": "kaun", "کیا": "kya", "کیوں": "kyun",
    "کہاں": "kahan", "کب": "kab", "کیسے": "kaise", "کیسا": "kaisa", "کیسی": "kaisi", "کتنا": "kitna",
    "کتنی": "kitni", "کتنے": "kitne", "جو": "jo", "جب": "jab", "تب": "tab", "یہاں": "yahan", "وہاں": "wahan",
    # postpositions / particles
    "کا": "ka", "کی": "ki", "کے": "ke", "کو": "ko", "سے": "se", "نے": "ne", "پر": "par", "تک": "tak",
    "لیے": "liye", "لئے": "liye", "ساتھ": "saath", "بعد": "baad", "پہلے": "pehle", "اور": "aur", "یا": "ya",
    "لیکن": "lekin", "مگر": "magar", "بھی": "bhi", "ہی": "hi", "نہ": "na", "نہیں": "nahin", "مت": "mat",
    "کہ": "keh", "اگر": "agar", "تو؟": "to?", "جی": "ji", "ہاں": "haan", "اچھا": "acha", "اچھی": "achi",
    "اچھے": "ache", "بہت": "bohat", "زیادہ": "zyada", "کم": "kam", "سب": "sab", "کچھ": "kuch", "کوئی": "koi",
    "ابھی": "abhi", "اب": "ab", "آج": "aaj", "کل": "kal", "پھر": "phir", "شاید": "shayad", "ضرور": "zaroor",
    "بالکل": "bilkul", "صرف": "sirf", "واقعی": "waqai", "شکریہ": "shukriya", "براہ": "barah", "کرم": "karam",
    # auxiliaries
    "ہے": "hai", "ہیں": "hain", "ہوں": "hoon", "ہو": "ho", "تھا": "tha", "تھی": "thi", "تھے": "the",
    "تھیں": "thin", "گا": "ga", "گی": "gi", "گے": "ge", "رہا": "raha", "رہی": "rahi", "رہے": "rahe",
    "چکا": "chuka", "چکی": "chuki", "چکے": "chuke", "سکتا": "sakta", "سکتی": "sakti", "سکتے": "sakte",
    # common verbs
    "کر": "kar", "کرو": "karo", "کرنا": "karna", "کریں": "karein", "کیجیے": "kijiye", "کرتا": "karta",
    "کرتی": "karti", "کرتے": "karte", "کیا؟": "kya?", "کی؟": "ki?", "ہوا": "hua", "ہوئی": "hui", "ہوئے": "hue",
    "جا": "ja", "جاؤ": "jao", "جانا": "jana", "جاتا": "jata", "جاتی": "jati", "جاتے": "jate", "گیا": "gaya",
    "گئی": "gayi", "گئے": "gaye", "آ": "aa", "آؤ": "aao", "آیا": "aaya", "آئی": "aayi", "آئے": "aaye",
    "آنا": "aana", "دے": "de", "دو": "do", "دینا": "dena", "دیا": "diya", "دی": "di", "لے": "le", "لو": "lo",
    "لینا": "lena", "لیا": "liya", "کہا": "kaha", "کہو": "kaho", "کہنا": "kehna", "بتاؤ": "batao",
    "سنو": "suno", "دیکھو": "dekho", "دیکھا": "dekha", "چلو": "chalo", "رکو": "ruko", "بیٹھو": "baitho",
    "چاہیے": "chahiye", "چاہتا": "chahta", "چاہتی": "chahti", "چاہتے": "chahte", "سمجھ": "samajh",
    "جانتا": "janta", "جانتی": "janti", "جانتے": "jante", "معلوم": "maloom", "پتہ": "pata", "ملا": "mila",
    "ملی": "mili", "ملے": "mile", "پیار": "pyar", "محبت": "mohabbat", "نکلو": "niklo", "نکل": "nikal",
    # common nouns / adjectives
    "گھر": "ghar", "بات": "baat", "کام": "kaam", "وقت": "waqt", "دن": "din", "رات": "raat", "دل": "dil",
    "آدمی": "aadmi", "عورت": "aurat", "بچہ": "bacha", "بچے": "bache", "ماں": "maan", "امی": "ammi",
    "ابو": "abbu", "باپ": "baap", "بھائی": "bhai", "بہن": "behen", "دوست": "dost", "یار": "yaar",
    "صاحب": "sahab", "جناب": "janab", "پیسے": "paise", "گاڑی": "gaari", "بازار": "bazaar", "پانی": "paani",
    "کھانا": "khana", "خدا": "khuda", "اللہ": "Allah", "حافظ": "hafiz", "ٹھیک": "theek", "خوش": "khush",
    "برا": "bura", "بڑا": "bara", "بڑی": "bari", "چھوٹا": "chhota", "نیا": "naya", "پرانا": "purana",
    "آسان": "aasaan", "مشکل": "mushkil", "جلدی": "jaldi", "فکر": "fikr", "ڈر": "dar", "سچ": "sach",
    "جھوٹ": "jhoot", "مدد": "madad", "زندگی": "zindagi", "دنیا": "duniya", "لوگ": "log", "نام": "naam", "انتظار": "intezaar", "بند": "band", "مذاق": "mazaaq",
    "دوسری": "doosri", "دوسرا": "doosra", "دوسرے": "doosre", "طرف": "taraf", "فوری": "fauri", "طور": "taur",
    "باہر": "bahar", "اندر": "andar", "سڑک": "sarak", "پار": "paar", "حاصل": "haasil", "کھیل": "khel",
    "ہاتھ": "haath", "بائیں": "baayen", "دائیں": "daayen", "تھکے": "thake", "تھکی": "thaki",
    "صبح": "subah", "شام": "shaam", "دیر": "der", "جلد": "jald", "مصیبت": "museebat", "آسمان": "aasmaan",
    "ساتویں": "saatwen", "اچانک": "achanak", "کبھی": "kabhi", "کبھار": "kabhar", "ٹکے": "takke", "سیر": "ser",
    "پتا": "pata", "انتظام": "intezaam", "ملک": "mulk", "شہر": "shehar", "خبر": "khabar", "سوال": "sawaal",
    "جواب": "jawaab", "غلط": "ghalat", "صحیح": "sahih", "پاگل": "pagal", "بار": "baar", "سو": "sau",
    "ساری": "saari", "سارا": "saara", "تیار": "tayyar", "قسمت": "qismat", "مبارک": "mubarak",
}

_ROMAN_CONSONANTS = {
    "ب": "b", "پ": "p", "ت": "t", "ٹ": "t", "ث": "s", "ج": "j", "چ": "ch", "ح": "h", "خ": "kh",
    "د": "d", "ڈ": "d", "ذ": "z", "ر": "r", "ڑ": "r", "ز": "z", "ژ": "zh", "س": "s", "ش": "sh",
    "ص": "s", "ض": "z", "ط": "t", "ظ": "z", "غ": "gh", "ف": "f", "ق": "q", "ک": "k", "گ": "g",
    "ل": "l", "م": "m", "ن": "n", "ہ": "h", "ۂ": "h", "ة": "h",
}
_ROMAN_PUNCT = {"؟": "?", "۔": ".", "،": ",", "؛": ";", "٪": "%"}
_URDU_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def _romanize_word(word: str) -> str:
    if word in ROMAN_LEXICON:
        return ROMAN_LEXICON[word]
    out: list[str] = []
    prev_consonant = False
    length = len(word)
    for index, char in enumerate(word):
        is_first = index == 0
        is_last = index == length - 1
        nxt = word[index + 1] if index + 1 < length else ""
        if char == "ھ":
            out.append("h")
            prev_consonant = True
            continue
        if char in "َ":
            out.append("a"); prev_consonant = False; continue
        if char in "ِ":
            out.append("i"); prev_consonant = False; continue
        if char in "ُ":
            out.append("u"); prev_consonant = False; continue
        if char == "ّ" and out:
            out.append(out[-1][-1]); continue
        if char in "ًٌٍْٰٔ":
            continue
        if char == "آ":
            out.append("aa"); prev_consonant = False; continue
        if char == "ا":
            out.append("a" if (is_first or is_last) else "aa"); prev_consonant = False; continue
        if char in "عء":
            if not out or not out[-1].endswith("a"):
                out.append("a")
            prev_consonant = False
            continue
        if char == "و":
            if is_first:
                out.append("w"); prev_consonant = True
            elif prev_consonant:
                out.append("o"); prev_consonant = False
            else:
                out.append("w" if nxt else "o"); prev_consonant = bool(nxt)
            continue
        if char == "ؤ":
            out.append("o"); prev_consonant = False; continue
        if char in "یئ":
            if is_first:
                out.append("y"); prev_consonant = True
            elif is_last:
                out.append("i"); prev_consonant = False
            elif not prev_consonant:
                out.append("y"); prev_consonant = True
            else:
                out.append("e" if nxt and nxt not in "اوے" else "i"); prev_consonant = False
            continue
        if char in "ےۓ":
            out.append("e"); prev_consonant = False; continue
        if char == "ں":
            out.append("n"); prev_consonant = False; continue
        if char in ("ہ", "ۂ") and is_last and index > 0:
            out.append("a" if prev_consonant else "h"); prev_consonant = False; continue
        roman = _ROMAN_CONSONANTS.get(char)
        if roman is None:
            out.append(char)
            prev_consonant = False
            continue
        if prev_consonant and not is_last:
            # Urdu omits short vowels; insert the most common one between clusters.
            out.append("a")
        elif prev_consonant and is_last and index >= 2:
            out.append("a")
        out.append(roman)
        prev_consonant = True
    return "".join(out)


def romanize_urdu(text: str, name_map: dict[str, str] | None = None) -> str:
    """Urdu script → Roman Urdu. ``name_map`` maps Urdu-script names to Latin."""
    if not text:
        return ""
    name_map = name_map or {}
    text = text.translate(_URDU_DIGITS)
    for urdu, latin in sorted(name_map.items(), key=lambda item: -len(item[0])):
        if urdu:
            text = text.replace(urdu, f"\u0001{latin}\u0002")
    # Letters only: Urdu punctuation (، ؛ ؟ ۔) lives in the same Unicode block
    # and must not be glued onto words.
    letters = "[ء-غف-ٰٟ-ۓەݐ-ݿ]"
    tokens = re.findall("\u0001[^\u0002]*\u0002|" + letters + "+|(?:(?!" + letters + ")[^\u0001])+", text)
    out: list[str] = []
    capitalize_next = True
    for token in tokens:
        if token.startswith("\u0001"):
            out.append(token[1:-1])
            capitalize_next = False
            continue
        if re.match(letters, token):
            romanized = _romanize_word(token)
            if capitalize_next and romanized:
                romanized = romanized[0].upper() + romanized[1:]
            out.append(romanized)
            capitalize_next = False
            continue
        piece = "".join(_ROMAN_PUNCT.get(char, char) for char in token)
        out.append(piece)
        if re.search(r"[.?!]\s*$", piece):
            capitalize_next = True
    return re.sub(r"\s+", " ", "".join(out)).strip()


# ---------------------------------------------------------------------------
# Syllable estimates for dubbing fit
# ---------------------------------------------------------------------------

_DEV_VOWELS = set("अआइईउऊऋएऐओऔ")
_DEV_MATRAS = set("ािीुूृेैोौ")
_DEV_VIRAMA = "्"


def english_syllables(text: str) -> int:
    total = 0
    for word in re.findall(r"[A-Za-z']+", text.lower()):
        groups = re.findall(r"[aeiouy]+", word)
        count = len(groups)
        if word.endswith("e") and count > 1 and not word.endswith(("le", "ee")):
            count -= 1
        total += max(1, count)
    total += len(re.findall(r"\d+", text)) * 2
    return total


def hindi_syllables(text: str) -> int:
    total = 0
    for word in re.findall(r"[ऀ-ॣ०-ॿ]+", text):
        count = 0
        chars = list(word)
        for index, char in enumerate(chars):
            nxt = chars[index + 1] if index + 1 < len(chars) else ""
            if char in _DEV_VOWELS:
                count += 1
            elif "क" <= char <= "ह" or char in "क़ख़ग़ज़ड़ढ़फ़य़":
                if nxt == _DEV_VIRAMA:
                    continue
                if nxt in _DEV_MATRAS:
                    count += 1
                elif index == len(chars) - 1 or nxt in "ंँ" and index + 1 == len(chars) - 1:
                    # word-final inherent schwa is usually deleted
                    count += 0 if count else 1
                else:
                    count += 1
        total += max(1, count)
    return total


def urdu_syllables(text: str) -> int:
    total = 0
    for word in re.findall(r"[ء-غف-ٰٟ-ۓە]+", text):
        if word in ROMAN_LEXICON:
            total += max(1, len(re.findall(r"[aeiou]+", ROMAN_LEXICON[word])))
            continue
        letters = [char for char in word if char not in "ھںءًٌٍَُِّْ"]
        vowels = sum(1 for char in letters if char in "اآویےئ")
        consonants = len(letters) - vowels
        total += max(1, round(vowels + consonants * 0.45))
    return total


def target_syllables(text: str, language: str) -> int:
    return hindi_syllables(text) if language == "hi" else urdu_syllables(text)


# ---------------------------------------------------------------------------
# Quality checks
# ---------------------------------------------------------------------------


def flag(code: str, severity: str, message: str) -> dict:
    return {"code": code, "severity": severity, "message": message}


def quality_flags(
    source: str,
    target: str,
    language: str,
    allowed_latin: set[str] | None = None,
) -> list[dict]:
    flags: list[dict] = []
    stripped = (target or "").strip()
    if not stripped:
        return [flag("empty", "error", "Translation is empty.")]
    script = URDU_RANGE if language == "ur" else DEVANAGARI_RANGE
    if not script.search(stripped):
        flags.append(flag("wrong_script", "error", "No characters in the expected target script."))
    if re.search(r"(?<![A-Za-z0-9])[NM]\d+X(?![A-Za-z0-9])|<\s*[A-Z]+\d*\s*>", stripped):
        flags.append(flag("placeholder_leak", "error", "An internal placeholder leaked into the output."))
    source_words = len(re.findall(r"[A-Za-z']+", source))
    src_syll = english_syllables(source)
    tgt_syll = target_syllables(stripped, language)
    if source_words >= 4 and src_syll and tgt_syll / src_syll < 0.35:
        flags.append(flag("incomplete", "warning", "Translation looks much shorter than the source; parts may be missing."))
    tokens = stripped.split()
    for index in range(len(tokens) - 3):
        if tokens[index] == tokens[index + 1] == tokens[index + 2] == tokens[index + 3]:
            flags.append(flag("repetition", "warning", "Repeated words suggest a degenerate model output."))
            break
    else:
        # repeated n-gram loops ("a b c a b c a b c")
        for size in (2, 3, 4):
            grams = [" ".join(tokens[i : i + size]) for i in range(0, max(0, len(tokens) - size + 1))]
            if any(grams.count(gram) >= 3 for gram in set(grams)) and len(tokens) > size * 3:
                flags.append(flag("repetition", "warning", "Repeated phrase suggests a degenerate model output."))
                break
    allowed = {word.lower() for word in (allowed_latin or set())}
    leftover = [word for word in LATIN_WORD.findall(stripped) if word.lower() not in allowed]
    if leftover:
        flags.append(
            flag("untranslated", "warning", f"Untranslated English left in output: {', '.join(leftover[:5])}")
        )
    if source.count('"') % 2 == 0 and stripped.count('"') % 2 == 1:
        flags.append(flag("unbalanced_quotes", "info", "Unbalanced quotation marks."))
    return flags


HUMOR_PATTERNS = [
    re.compile(r"\bwhy did (the|a)\b.*\?", re.I),
    re.compile(r"\b(knock,? knock|that's what she said|get it\?|pun intended|just kidding|i'm kidding)\b", re.I),
    re.compile(r"\b(joke|joking|funny|hilarious|punchline)\b", re.I),
]


def looks_like_humor(text: str) -> bool:
    return any(pattern.search(text) for pattern in HUMOR_PATTERNS)
