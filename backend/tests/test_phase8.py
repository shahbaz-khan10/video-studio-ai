"""Phase 8 — context-aware translation engine and editor API.

The default suite uses a deterministic stand-in for the MT model whose outputs
mirror what NLLB-200 actually produced for these lines (captured during
evaluation), so the context/post-editing layer is tested on realistic text.
Set RUN_REAL_MT=1 to also run the real-model tests (downloads ~2.5 GB once).
"""

import os
import re
import time

import pytest
from sqlalchemy import select

import app.services.translation as translation_service
import app.services.translation_engine as engine
from app.database import SessionLocal
from app.models import (
    Character,
    CharacterAssignment,
    ProcessingJob,
    Transcript,
    TranscriptSegment,
    Video,
)
from app.services import translation_lang as lang
from app.services.storage import storage

# ---------------------------------------------------------------------------
# Fake MT engine (shapes taken from real NLLB-200-distilled-600M outputs)
# ---------------------------------------------------------------------------

UR = {
    "N1X, where are you going?": "N1X، آپ کہاں جا رہے ہیں؟",
    "Where are you going?": "تم کہاں جا رہے ہو؟",
    "I am going to the market.": "میں بازار جا رہا ہوں.",
    "I was waiting for you!": "میں آپ کا انتظار کر رہا تھا!",
    "Don't worry, it's M1X.": "فکر نہ کرو، یہ M1X ہے.",
    "Don't worry, it's very easy.": "فکر نہ کرو، یہ بہت آسان ہے.",
    "Mother, are you coming with us?": "ماں، آپ ہمارے ساتھ آ رہے ہیں؟",
    "N1X, I will come tomorrow.": "N1X، میں کل آؤں گا.",
    "Are you tired?": "کیا تم تھکے ہوئے ہو؟",
    "Why did the chicken cross the road?": "چکن نے سڑک کیوں پار کی؟",
    "To get to the other side!": "دوسری طرف حاصل کرنے کے لئے!",
    "N1X, stop joking with me.": "N1X، مجھ سے مذاق بند کرو.",
    "N1X, get out of my house right now!": "N1X، فوری طور پر میرے گھر سے باہر جاؤ!",
    "Hello.": "ہیلو.",
    "Sir, the car is ready.": "سر، گاڑی تیار ہے.",
    "Tell N1X that I am coming.": "N1X کو بتائیں کہ میں آ رہا ہوں.",
}
HI = {
    "N1X, where are you going?": "N1X, तुम कहाँ जा रहे हो?",
    "Where are you going?": "तुम कहाँ जा रहे हो?",
    "I am going to the market.": "मैं बाजार जा रहा हूँ.",
    "I was waiting for you!": "मैं आपका इंतजार कर रहा था!",
    "Don't worry, it's M1X.": "चिंता मत करो, यह M1X है.",
    "Don't worry, it's very easy.": "चिंता मत करो, यह बहुत आसान है.",
    "Mother, are you coming with us?": "माँ, क्या आप हमारे साथ आ रहे हैं?",
    "N1X, I will come tomorrow.": "N1X, मैं कल आ रहा हूँ.",
    "Are you tired?": "क्या आप थक गए हैं?",
    "Why did the chicken cross the road?": "चिकन सड़क पार क्यों किया?",
    "To get to the other side!": "दूसरी तरफ जाने के लिए!",
    "N1X, stop joking with me.": "N1X, मेरे साथ मजाक करना बंद करो.",
    "N1X, get out of my house right now!": "N1X, मेरे घर से बाहर जाओ अभी!",
    "Hello.": "नमस्ते.",
    "Sir, the car is ready.": "सर, कार तैयार है।",
    "Tell N1X that I am coming.": "N1X को बताओ कि मैं आ रहा हूँ।",
}
NAMES = {"ur": {"Ali": "علی", "Sara": "سارہ", "Zainab": "زینب", "Zorbek": "زوربیک"},
         "hi": {"Ali": "अली", "Sara": "सारा", "Zainab": "ज़ैनब", "Zorbek": "ज़ोरबेक"}}
FILLER = {"ur": "لفظ", "hi": "शब्द"}


class FakeMT:
    def __init__(self):
        self.calls: list[tuple[str, list[str]]] = []
        self.fail_after: int | None = None
        self.drop_placeholders = False

    def __call__(self, texts, target, check_active=None, backend=None):
        if check_active:
            check_active()
        if self.fail_after is not None:
            if self.fail_after <= 0:
                raise engine.TranslationUnavailable("simulated GPU worker crash")
            self.fail_after -= 1
        self.calls.append((target, list(texts)))
        table = UR if target == "ur" else HI
        out = []
        for text in texts:
            match = re.fullmatch(r"My name is (.+)\.", text)
            if match:
                name = NAMES[target].get(match.group(1))
                out.append((f"میرا نام {name} ہے." if target == "ur" else f"मेरा नाम {name} है।") if name else text)
                continue
            if text in table:
                result = table[text]
            else:
                tokens = re.findall(r"[NM]\d+X", text)
                words = max(1, len(re.findall(r"[A-Za-z']+", text)) - len(tokens))
                result = " ".join(tokens + [FILLER[target]] * words) + (text[-1] if text[-1] in "?!." else "")
            if self.drop_placeholders:
                result = re.sub(r"[NM]\d+X", "", result).strip()
            out.append(result)
        return out


@pytest.fixture
def fake_mt(monkeypatch):
    fake = FakeMT()
    monkeypatch.setattr(engine, "translate_texts", fake)
    translation_service._MODEL_SPELLINGS.clear()
    return fake


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------

SCENE = [
    ("Ali", "Sara, where are you going?"),
    ("Sara", "I am going to the market. I was waiting for you!"),
    ("Ali", "Break a leg tonight, Sara!"),
    ("Sara", "Don't worry, it's a piece of cake."),
    ("Sara", "Mother, are you coming with us?"),
    ("Zainab", "Sara, I will come tomorrow. Are you tired?"),
    ("Ali", "Why did the chicken cross the road? To get to the other side!"),
    ("Sara", "Ali, stop pulling my leg."),
    ("Zainab", "Ali, get out of my house right now!"),
]


def seed(client, lines=SCENE, with_file=False, gap_ms=600, duration_ms=2400):
    project = client.post(
        "/api/v1/projects", json={"name": "Phase 8 film", "source_language": "en", "target_languages": ["ur", "hi"]}
    ).json()
    with SessionLocal() as db:
        key = f"projects/{project['id']}/source.mp4"
        if with_file:
            storage.write_bytes(key, b"\x00" * 64)
        video = Video(
            project_id=project["id"], original_filename="film.mp4", storage_key=key,
            content_type="video/mp4", size_bytes=64, duration_ms=len(lines) * (duration_ms + gap_ms) + 10_000,
        )
        db.add(video)
        db.flush()
        transcript = Transcript(project_id=project["id"], video_id=video.id, language="en", status="completed")
        db.add(transcript)
        db.flush()
        characters: dict[str, Character] = {}
        segments = []
        cursor = 0
        for position, (name, text) in enumerate(lines):
            segment = TranscriptSegment(
                transcript_id=transcript.id, position=position, start_ms=cursor, end_ms=cursor + duration_ms,
                text=text, words=[], language="en",
            )
            cursor += duration_ms + gap_ms
            db.add(segment)
            db.flush()
            segments.append(segment.id)
            if name:
                if name not in characters:
                    characters[name] = Character(project_id=project["id"], name=name, status="confirmed")
                    db.add(characters[name])
                    db.flush()
                db.add(CharacterAssignment(character_id=characters[name].id, segment_id=segment.id, video_id=video.id))
        db.commit()
        return {
            "project_id": project["id"],
            "video_id": video.id,
            "transcript_id": transcript.id,
            "segments": segments,
            "characters": {name: item.id for name, item in characters.items()},
        }


def configure_scene(client, ids):
    chars = ids["characters"]
    for name, payload in {
        "Ali": {"gender": "male", "register": "informal", "speaking_style": "cocky young man"},
        "Sara": {"gender": "female", "register": "auto"},
        "Zainab": {"gender": "female", "register": "informal", "speaking_style": "strict mother"},
    }.items():
        response = client.put(f"/api/v1/characters/{chars[name]}/translation-profile", json=payload)
        assert response.status_code == 200, response.text
    response = client.put(
        f"/api/v1/projects/{ids['project_id']}/character-relationships",
        json={"relationships": [
            {"speaker_character_id": chars["Sara"], "listener_character_id": chars["Zainab"], "address_form": "formal", "relation": "daughter to mother"},
            {"speaker_character_id": chars["Ali"], "listener_character_id": chars["Sara"], "address_form": "informal", "relation": "friends"},
        ]},
    )
    assert response.status_code == 200, response.text


def wait_job(client, job_id, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/v1/jobs/{job_id}").json()
        if job["status"] in {"completed", "failed", "cancelled"}:
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def line(bundle, segment_id):
    return next(item for item in bundle["lines"] if item["segment_id"] == segment_id)


def translate_all(client, ids, **payload):
    response = client.post(f"/api/v1/videos/{ids['video_id']}/translations/translate", json=payload)
    assert response.status_code == 202, response.text
    return wait_job(client, response.json()["id"])


# ---------------------------------------------------------------------------
# Language-layer unit tests
# ---------------------------------------------------------------------------


def test_register_conversion_urdu_and_hindi():
    assert lang.apply_register("آپ کہاں جا رہے ہیں؟", "ur", "informal")[0] == "تم کہاں جا رہے ہو؟"
    assert lang.apply_register("تم کہاں جا رہے ہو؟", "ur", "formal")[0] == "آپ کہاں جا رہے ہیں؟"
    assert lang.apply_register("आप कहाँ जा रहे हैं?", "hi", "informal")[0] == "तुम कहाँ जा रहे हो?"
    assert lang.apply_register("तुम कहाँ जा रहे हो?", "hi", "formal")[0] == "आप कहाँ जा रहे हैं?"
    assert lang.apply_register("तुम कहाँ जा रहे हो?", "hi", "intimate")[0] == "तू कहाँ जा रहा है?"
    assert lang.apply_register("आपका नाम क्या है?", "hi", "informal")[0] == "तुम्हारा नाम क्या है?"
    assert lang.apply_register("آپ کا نام کیا ہے؟", "ur", "informal")[0] == "تمہارا نام کیا ہے؟"
    assert lang.apply_register("यहाँ बैठिए।", "hi", "informal")[0].startswith("यहाँ")
    # Neutral leaves model output untouched.
    assert lang.apply_register("आप कहाँ जा रहे हैं?", "hi", "neutral") == ("आप कहाँ जा रहे हैं?", [])
    # Postposition में must not be mistaken for a polite verb ending.
    assert "में" in lang.apply_register("आप घर में हैं", "hi", "informal")[0]


def test_speaker_and_listener_gender_agreement():
    assert lang.apply_speaker_gender("میں بازار جا رہا ہوں.", "ur", "female")[0] == "میں بازار جا رہی ہوں."
    assert lang.apply_speaker_gender("मैं बाजार जा रहा हूँ.", "hi", "female")[0] == "मैं बाजार जा रही हूँ."
    assert lang.apply_speaker_gender("मैं कल आऊँगा।", "hi", "female")[0] == "मैं कल आऊँगी।"
    assert lang.apply_speaker_gender("میں کل آؤں گا.", "ur", "female")[0] == "میں کل آؤں گی."
    assert lang.apply_speaker_gender("मैं इंतजार कर रहा था!", "hi", "female")[0] == "मैं इंतजार कर रही थी!"
    # Third-person past must not be feminised for a female speaker.
    assert lang.apply_speaker_gender("वह घर गया था।", "hi", "female")[0] == "वह घर गया था।"
    assert lang.apply_listener_gender("کیا تم تھکے ہوئے ہو؟", "ur", "female")[0] == "کیا تم تھکی ہوئی ہو؟"
    assert lang.apply_listener_gender("तुम कहाँ जा रहे हो?", "hi", "female")[0] == "तुम कहाँ जा रही हो?"
    assert lang.apply_listener_gender("आप आ रहे हैं?", "hi", "female")[0] == "आप आ रही हैं?"
    assert lang.apply_listener_gender("تم کہاں جا رہے ہو؟", "ur", "male")[1] == []


def test_punctuation_is_preserved_in_target_script():
    assert lang.preserve_punctuation("Where are you?", "تم کہاں ہو?", "ur")[0] == "تم کہاں ہو؟"
    assert lang.preserve_punctuation("I am here.", "میں یہاں ہوں.", "ur")[0] == "میں یہاں ہوں۔"
    assert lang.preserve_punctuation("I am here.", "मैं यहाँ हूँ.", "hi")[0] == "मैं यहाँ हूँ।"
    assert lang.preserve_punctuation("Run!", "भागो।", "hi")[0] == "भागो!"
    assert lang.preserve_punctuation("Wait. Stop!", "रुको. बस!", "hi")[0] == "रुको। बस!"
    assert lang.preserve_punctuation("Yes, sir.", "جی, سر.", "ur")[0] == "جی، سر۔"
    # A fragment does not gain an invented full stop.
    assert lang.preserve_punctuation("and then we", "اور پھر ہم۔", "ur")[0] == "اور پھر ہم"


def test_roman_urdu_transliteration():
    assert lang.romanize_urdu("میں بازار جا رہی ہوں۔") == "Main bazaar ja rahi hoon."
    assert lang.romanize_urdu("سارہ، تم کہاں جا رہی ہو؟", {"سارہ": "Sara"}) == "Sara, tum kahan ja rahi ho?"
    assert lang.romanize_urdu("فکر نہ کرو، یہ بائیں ہاتھ کا کھیل ہے۔") == "Fikr na karo, yeh baayen haath ka khel hai."
    unknown = lang.romanize_urdu("گھر")
    assert unknown == "Ghar"
    assert re.fullmatch(r"[A-Za-z ,.?']+", lang.romanize_urdu("کتاب پڑھو"))


def test_quality_flags_detect_problems():
    codes = lambda src, tgt, lg="ur": {item["code"] for item in lang.quality_flags(src, tgt, lg)}
    assert codes("Hello there.", "") == {"empty"}
    assert "wrong_script" in codes("Hello there.", "Hello there.")
    assert "incomplete" in codes("I went to the market yesterday to buy fresh vegetables for dinner.", "میں")
    assert "repetition" in codes("Go.", "جاؤ جاؤ جاؤ جاؤ جاؤ")
    assert "placeholder_leak" in codes("Tell Ali.", "N1X کو بتاؤ")
    assert "untranslated" in codes("The spaceship landed.", "spaceship اتر گیا")
    assert codes("The spaceship landed.", "Zorbek اتر گیا") - {"untranslated"} == set()
    assert not ({"untranslated"} & {item["code"] for item in lang.quality_flags("Zorbek landed.", "Zorbek اتر گیا", "ur", {"Zorbek"})})
    assert codes("Hello.", "ہیلو۔") == set()


def test_dubbing_fit_flags_long_lines():
    fit = translation_service.dubbing_fit("Go!", "جاؤ!", "ur", 0, 1000)
    assert fit["status"] == "ok"
    long = translation_service.dubbing_fit(
        "Go!", "براہ کرم فوری طور پر یہاں سے چلے جائیں اور کبھی واپس نہ آئیں", "ur", 0, 600
    )
    assert long["status"] == "long" and long["estimated_s"] > long["duration_s"]
    assert translation_service.dubbing_fit("Hi", "", "hi", 0, 500)["status"] == "empty"


def test_idioms_adapted_not_literal():
    text, natives, used = translation_service._apply_idioms("Don't worry, it's a piece of cake.", "ur")
    assert text == "Don't worry, it's M1X." and natives["M1X"] == "بائیں ہاتھ کا کھیل"
    text, natives, used = translation_service._apply_idioms("Break a leg tonight!", "hi")
    assert "leg" not in text and "luck" in text and used[0]["strategy"] == "meaning"
    assert translation_service._apply_idioms("She broke her leg.", "hi")[2] == []


def test_name_transliteration_fallback():
    assert lang.DEVANAGARI_RANGE.search(translation_service.transliterate_name("Zorbek", "hi"))
    assert lang.URDU_RANGE.search(translation_service.transliterate_name("Zorbek", "ur"))
    assert translation_service.transliterate_name("Sara", "hi").endswith("ा")


def test_engine_reports_models_licenses_and_requirements(client):
    info = client.get("/api/v1/translation/engine").json()
    assert info["backend"] in {"nllb", "indictrans2", "madlad", "remote"}
    assert info["backends"]["nllb"]["license"] == "CC-BY-NC-4.0"
    assert info["backends"]["nllb"]["commercial_use"] is False
    assert info["backends"]["indictrans2"]["license"] == "MIT"
    assert info["backends"]["madlad"]["license"] == "Apache-2.0"
    assert info["requirements"]["ram_cpu_gb"] > 0
    system = client.get("/api/v1/info").json()
    assert system["capabilities"]["translation"] is True
    assert system["capabilities"]["translation_editor"] is True
    assert system["translation"]["backend"] == info["backend"]


# ---------------------------------------------------------------------------
# API / context tests
# ---------------------------------------------------------------------------


def test_context_aware_translation_of_scene(client, fake_mt):
    ids = seed(client)
    configure_scene(client, ids)
    job = translate_all(client, ids)
    assert job["status"] == "completed", job
    bundle = client.get(f"/api/v1/videos/{ids['video_id']}/translations").json()
    seg = ids["segments"]

    first = line(bundle, seg[0])["translations"]
    # Ali → Sara are friends: model's formal "آپ ... رہے ہیں" becomes "تم ... رہی ہو" (female listener).
    assert first["ur"]["text"] == "سارہ، تم کہاں جا رہی ہو؟"
    assert first["hi"]["text"] == "सारा, तुम कहाँ जा रही हो?"
    assert first["ur"]["context"]["register_reason"].startswith("relationship")
    assert first["ur"]["roman_text"] == "Sara, tum kahan ja rahi ho?"
    assert first["ur"]["origin"] == "ai"

    second = line(bundle, seg[1])["translations"]
    # Sara is female: first-person agreement fixed in both languages.
    assert second["ur"]["text"] == "میں بازار جا رہی ہوں۔ میں آپ کا انتظار کر رہی تھی!"
    assert second["hi"]["text"] == "मैं बाजार जा रही हूँ। मैं आपका इंतजार कर रही थी!"

    idiom = line(bundle, seg[3])["translations"]
    assert idiom["ur"]["text"] == "فکر نہ کرو، یہ بائیں ہاتھ کا کھیل ہے۔"
    assert idiom["hi"]["text"] == "चिंता मत करो, यह बाएँ हाथ का खेल है।"
    assert "idiom_adapted" in {item["code"] for item in idiom["ur"]["qc_flags"]}

    mother = line(bundle, seg[4])["translations"]
    # "Mother," + relationship "daughter to mother" (formal) + female listener.
    assert mother["ur"]["text"] == "ماں، آپ ہمارے ساتھ آ رہی ہیں؟"
    assert mother["ur"]["context"]["listener"]["reason"] == "kinship form of address"

    reply = line(bundle, seg[5])["translations"]
    assert reply["ur"]["text"] == "سارہ، میں کل آؤں گی۔ کیا تم تھکی ہوئی ہو؟"
    assert reply["hi"]["text"] == "सारा, मैं कल आ रही हूँ। क्या तुम थक गई हो?"

    joke = line(bundle, seg[6])["translations"]
    assert "humor_review" in {item["code"] for item in joke["ur"]["qc_flags"]}

    for item in bundle["lines"]:
        # English transcript remains unchanged.
        assert item["text"] == SCENE[item["position"]][1]


def test_character_names_stay_consistent(client, fake_mt):
    ids = seed(client)
    translate_all(client, ids)
    bundle = client.get(f"/api/v1/videos/{ids['video_id']}/translations").json()
    sara_lines = [item for item in bundle["lines"] if "Sara" in item["text"]]
    assert len(sara_lines) >= 3
    for item in sara_lines:
        assert "سارہ" in item["translations"]["ur"]["text"]
        assert "सारा" in item["translations"]["hi"]["text"]
        assert "Sara" in item["translations"]["ur"]["roman_text"]
    profile = next(item for item in bundle["profiles"] if item["name"] == "Sara")
    assert profile["name_ur"] == "سارہ" and profile["name_hi"] == "सारा" and profile["names_auto"] is True

    # A user-chosen spelling is then used everywhere on the next pass.
    client.put(
        f"/api/v1/characters/{ids['characters']['Sara']}/translation-profile",
        json={"gender": "female", "register": "auto", "name_ur": "سارا", "name_hi": "साराह", "name_roman": "Saara"},
    )
    translate_all(client, ids, languages=["ur", "hi"])
    bundle = client.get(f"/api/v1/videos/{ids['video_id']}/translations").json()
    for item in [entry for entry in bundle["lines"] if "Sara" in entry["text"]]:
        assert "سارا" in item["translations"]["ur"]["text"]
        assert "साराह" in item["translations"]["hi"]["text"]
        assert "Saara" in item["translations"]["ur"]["roman_text"]


def test_lost_placeholder_falls_back_and_repairs_names(client, fake_mt):
    ids = seed(client, [("Ali", "Sara, where are you going?"), ("Sara", "Hello.")])
    fake_mt.drop_placeholders = True
    response = client.post(
        f"/api/v1/videos/{ids['video_id']}/translations/segments/{ids['segments'][0]}/translate",
        json={"languages": ["hi"]},
    )
    assert response.status_code == 200, response.text
    hi = response.json()["translations"]["hi"]
    assert hi["context"]["placeholder_fallback"] == ["N1X"]
    assert "placeholder_leak" not in {item["code"] for item in hi["qc_flags"]}


def test_glossary_terms_are_locked(client, fake_mt):
    ids = seed(client, [(None, "Tell Zorbek that I am coming.")])
    response = client.put(
        f"/api/v1/projects/{ids['project_id']}/translation-glossary",
        json={"terms": [{"source": "Zorbek", "ur": "زوربیک", "hi": "ज़ोरबेक", "roman": "Zorbek"}]},
    )
    assert response.status_code == 200
    result = client.post(
        f"/api/v1/videos/{ids['video_id']}/translations/segments/{ids['segments'][0]}/translate",
        json={"languages": ["ur", "hi"]},
    ).json()["translations"]
    assert result["ur"]["text"].startswith("زوربیک کو")
    assert result["hi"]["text"].startswith("ज़ोरबेक को")
    assert result["ur"]["roman_text"].startswith("Zorbek")
    duplicate = client.put(
        f"/api/v1/projects/{ids['project_id']}/translation-glossary",
        json={"terms": [{"source": "Zorbek"}, {"source": "zorbek"}]},
    )
    assert duplicate.status_code == 422


def test_manual_edit_is_saved_and_never_silently_overwritten(client, fake_mt):
    ids = seed(client)
    translate_all(client, ids, languages=["ur"])
    seg = ids["segments"][0]
    bundle = client.get(f"/api/v1/videos/{ids['video_id']}/translations").json()
    before = line(bundle, seg)["translations"]["ur"]
    edited = client.patch(
        f"/api/v1/translations/{seg}/ur",
        json={"text": "سارہ، کدھر جا رہی ہو؟", "base_revision": before["revision"]},
    )
    assert edited.status_code == 200, edited.text
    ur = edited.json()["translations"]["ur"]
    assert ur["origin"] == "manual" and ur["status"] == "edited"
    assert ur["text"] == "سارہ، کدھر جا رہی ہو؟"
    assert ur["roman_text"].startswith("Sara")
    assert ur["history"][-1]["text"] == before["text"]

    # Stale base revision → conflict, nothing lost.
    conflict = client.patch(f"/api/v1/translations/{seg}/ur", json={"text": "x", "base_revision": before["revision"]})
    assert conflict.status_code == 409

    # Translate-all keeps the manual text; the new AI output is only a suggestion.
    translate_all(client, ids, languages=["ur"])
    again = line(client.get(f"/api/v1/videos/{ids['video_id']}/translations").json(), seg)["translations"]["ur"]
    assert again["text"] == "سارہ، کدھر جا رہی ہو؟" and again["origin"] == "manual"
    assert again["suggestion_available"] is True and again["ai_text"] == before["text"]

    # Regenerating a single manual line without force also keeps it.
    kept = client.post(
        f"/api/v1/videos/{ids['video_id']}/translations/segments/{seg}/translate", json={"languages": ["ur"]}
    ).json()["translations"]["ur"]
    assert kept["text"] == "سارہ، کدھر جا رہی ہو؟"

    # Explicit force replaces it, and the manual version stays in history.
    forced = client.post(
        f"/api/v1/videos/{ids['video_id']}/translations/segments/{seg}/translate",
        json={"languages": ["ur"], "force": True},
    ).json()["translations"]["ur"]
    assert forced["origin"] == "ai" and forced["text"] == before["text"]
    assert any(entry["text"] == "سارہ، کدھر جا رہی ہو؟" for entry in forced["history"])


def test_undo_restore_and_roman_editing(client, fake_mt):
    ids = seed(client)
    translate_all(client, ids, languages=["ur"])
    seg = ids["segments"][1]
    original = line(client.get(f"/api/v1/videos/{ids['video_id']}/translations").json(), seg)["translations"]["ur"]
    client.patch(f"/api/v1/translations/{seg}/ur", json={"text": "میں بازار جا رہی ہوں۔"})
    # Undo by typing back the AI text returns the line to AI status.
    undone = client.patch(f"/api/v1/translations/{seg}/ur", json={"text": original["text"]}).json()["translations"]["ur"]
    assert undone["origin"] == "ai"
    # Restore a history entry (redo).
    history = undone["history"]
    index = max(i for i, entry in enumerate(history) if entry["text"] == "میں بازار جا رہی ہوں۔")
    redone = client.post(f"/api/v1/translations/{seg}/ur/restore", json={"target": "history", "index": index}).json()
    assert redone["translations"]["ur"]["text"] == "میں بازار جا رہی ہوں۔"
    restored = client.post(f"/api/v1/translations/{seg}/ur/restore", json={"target": "ai"}).json()
    assert restored["translations"]["ur"]["text"] == original["text"]

    # Roman Urdu is edited independently and survives Urdu edits.
    roman = client.patch(f"/api/v1/translations/{seg}/ur", json={"roman_text": "Main bazaar ja rahi hoon, intezaar tha!"}).json()
    assert roman["translations"]["ur"]["roman_origin"] == "manual"
    client.patch(f"/api/v1/translations/{seg}/ur", json={"text": "میں بازار جا رہی ہوں۔"})
    after = line(client.get(f"/api/v1/videos/{ids['video_id']}/translations").json(), seg)["translations"]["ur"]
    assert after["roman_text"] == "Main bazaar ja rahi hoon, intezaar tha!"
    regenerated = client.post(f"/api/v1/translations/{seg}/ur/romanize").json()["translations"]["ur"]
    assert regenerated["roman_origin"] == "auto" and regenerated["roman_text"] == "Main bazaar ja rahi hoon."
    assert client.patch(f"/api/v1/translations/{seg}/hi", json={"roman_text": "x"}).status_code == 422


def test_hand_written_translation_without_ai_pass(client, fake_mt):
    ids = seed(client, [("Ali", "Hello.")])
    seg = ids["segments"][0]
    response = client.patch(f"/api/v1/translations/{seg}/hi", json={"text": "नमस्ते।"})
    assert response.status_code == 200
    hi = response.json()["translations"]["hi"]
    assert hi["origin"] == "manual" and hi["text"] == "नमस्ते।"
    assert response.json()["translations"]["ur"] is None


def test_empty_translation_is_flagged(client, fake_mt):
    ids = seed(client, [("Ali", "Hello.")])
    seg = ids["segments"][0]
    client.post(f"/api/v1/videos/{ids['video_id']}/translations/segments/{seg}/translate", json={"languages": ["ur"]})
    emptied = client.patch(f"/api/v1/translations/{seg}/ur", json={"text": ""}).json()["translations"]["ur"]
    assert emptied["status"] == "empty"
    assert [item["code"] for item in emptied["qc_flags"]][0] == "empty"


def test_stale_translation_after_transcript_edit(client, fake_mt):
    ids = seed(client, [("Ali", "Hello."), ("Sara", "Where are you going?")])
    translate_all(client, ids)
    transcript = client.get(f"/api/v1/videos/{ids['video_id']}/transcript").json()
    segments = [
        {"id": item["id"], "start_ms": item["start_ms"], "end_ms": item["end_ms"], "text": item["text"], "speaker": item["speaker"]}
        for item in transcript["segments"]
    ]
    segments[0]["text"] = "Hello, my friend."
    saved = client.put(
        f"/api/v1/videos/{ids['video_id']}/transcript", json={"revision": transcript["revision"], "segments": segments}
    )
    assert saved.status_code == 200, saved.text
    bundle = client.get(f"/api/v1/videos/{ids['video_id']}/translations").json()
    first = line(bundle, ids["segments"][0])["translations"]["ur"]
    assert first["stale"] is True and first["status"] == "stale"
    assert first["qc_flags"][0]["code"] == "source_changed"
    assert bundle["summary"]["ur"]["stale"] == 1
    # only_missing retranslates stale lines but not up-to-date ones.
    fake_mt.calls.clear()
    translate_all(client, ids, languages=["ur"], only_missing=True)
    sent = [text for target, texts in fake_mt.calls[1:] for text in texts if not text.startswith("My name is")]
    assert sent == ["Hello, my friend."]  # calls[0] is the model warm-up


def test_translation_memory_reuses_corrections(client, fake_mt):
    ids = seed(client, [("Ali", "Sir, the car is ready."), ("Sara", "Hello."), ("Ali", "Sir, the car is ready.")])
    seg = ids["segments"]
    client.post(f"/api/v1/videos/{ids['video_id']}/translations/segments/{seg[0]}/translate", json={"languages": ["ur"]})
    client.patch(f"/api/v1/translations/{seg[0]}/ur", json={"text": "صاحب، گاڑی تیار ہے۔"})
    result = client.post(
        f"/api/v1/videos/{ids['video_id']}/translations/segments/{seg[2]}/translate", json={"languages": ["ur"]}
    ).json()["translations"]["ur"]
    assert result["text"] == "صاحب، گاڑی تیار ہے۔"
    assert result["origin"] == "memory"


def test_scene_notes_and_scene_detection(client, fake_mt):
    lines = [("Ali", "Hello."), ("Sara", "Hello."), ("Ali", "Where are you going?")]
    ids = seed(client, lines, gap_ms=7000)
    bundle = client.get(f"/api/v1/videos/{ids['video_id']}/translations").json()
    assert [item["scene_index"] for item in bundle["lines"]] == [0, 1, 2]
    note = client.put(
        f"/api/v1/videos/{ids['video_id']}/translation-scenes/{ids['segments'][1]}",
        json={"note": "Night, hospital corridor, tense"},
    )
    assert note.status_code == 200
    result = client.post(
        f"/api/v1/videos/{ids['video_id']}/translations/segments/{ids['segments'][1]}/translate", json={"languages": ["hi"]}
    ).json()
    assert result["translations"]["hi"]["context"]["scene_note"] == "Night, hospital corridor, tense"


def test_relationship_validation(client, fake_mt):
    ids = seed(client)
    chars = ids["characters"]
    bad = client.put(
        f"/api/v1/projects/{ids['project_id']}/character-relationships",
        json={"relationships": [{"speaker_character_id": chars["Ali"], "listener_character_id": chars["Ali"]}]},
    )
    assert bad.status_code == 422
    unknown = client.put(
        f"/api/v1/projects/{ids['project_id']}/character-relationships",
        json={"relationships": [{"speaker_character_id": chars["Ali"], "listener_character_id": "nope"}]},
    )
    assert unknown.status_code == 422


def test_translate_requires_transcript_and_language(client, fake_mt):
    project = client.post("/api/v1/projects", json={"name": "Empty", "target_languages": ["ur"]}).json()
    with SessionLocal() as db:
        video = Video(project_id=project["id"], original_filename="a.mp4", storage_key=f"projects/{project['id']}/a.mp4", content_type="video/mp4", size_bytes=1)
        db.add(video)
        db.commit()
        video_id = video.id
    assert client.post(f"/api/v1/videos/{video_id}/translations/translate", json={}).status_code == 404
    ids = seed(client)
    assert client.post(f"/api/v1/videos/{ids['video_id']}/translations/translate", json={"languages": ["fr"]}).status_code == 400
    assert client.get(f"/api/v1/videos/{video_id}/translations").json()["lines"] == []


def test_long_transcript_batches_and_progress(client, fake_mt, monkeypatch):
    monkeypatch.setattr(translation_service.settings, "translation_batch_size", 16)
    lines = [(("Ali", "Sara", "Zainab")[i % 3], f"Line number {i} of the long film, keep going.") for i in range(600)]
    ids = seed(client, lines, gap_ms=200, duration_ms=1800)
    started = time.time()
    job = translate_all(client, ids)
    elapsed = time.time() - started
    assert job["status"] == "completed", job
    assert elapsed < 60
    bundle = client.get(f"/api/v1/videos/{ids['video_id']}/translations").json()
    assert bundle["summary"]["ur"]["translated"] == 600
    assert bundle["summary"]["hi"]["translated"] == 600
    translate_step = next(step for step in job["steps"] if step["name"] == "translate")
    assert "600 translated" in translate_step["detail"] or "1200 translated" in translate_step["detail"]
    # Batching: no MT call larger than one batch of segments (plus sentence splits).
    assert max(len(texts) for _, texts in fake_mt.calls) <= 32


def test_failed_job_and_resume_on_retry(client, fake_mt, monkeypatch):
    monkeypatch.setattr(translation_service.settings, "translation_batch_size", 4)
    lines = [(("Ali", "Sara")[i % 2], f"Sentence {i} here.") for i in range(12)]
    ids = seed(client, lines, with_file=True)
    # Warm-up + two name lookups + first two batches succeed, then the "GPU worker" dies.
    fake_mt.fail_after = 1 + 2 + 2
    job = translate_all(client, ids, languages=["ur"])
    assert job["status"] == "failed"
    assert job["error_code"] == "TRANSLATION_ENGINE_UNAVAILABLE"
    with SessionLocal() as db:
        from app.models import SegmentTranslation

        done = db.scalars(select(SegmentTranslation).where(SegmentTranslation.video_id == ids["video_id"])).all()
        done_ids = {row.segment_id for row in done}
    assert 0 < len(done_ids) < 12

    fake_mt.fail_after = None
    fake_mt.calls.clear()
    retry = client.post(f"/api/v1/jobs/{job['id']}/retry")
    assert retry.status_code == 202, retry.text
    retried = wait_job(client, retry.json()["id"])
    assert retried["status"] == "completed", retried
    sent = [text for _, texts in fake_mt.calls for text in texts if text.startswith("Sentence")]
    # Lines finished before the crash are not translated again.
    assert len(sent) == 12 - len(done_ids)
    bundle = client.get(f"/api/v1/videos/{ids['video_id']}/translations").json()
    assert bundle["summary"]["ur"]["translated"] == 12


def test_cancel_translation_job(client, fake_mt):
    ids = seed(client)
    with SessionLocal() as db:
        video = db.get(Video, ids["video_id"])
        job = translation_service.create_translation_job(db, video, {"languages": ["ur"]})
        job_id = job.id
    assert client.post(f"/api/v1/jobs/{job_id}/cancel").status_code == 200
    translation_service.run_translation_job(job_id, ids["video_id"], {"languages": ["ur"]})
    with SessionLocal() as db:
        assert db.get(ProcessingJob, job_id).status == "cancelled"


def test_duplicate_active_job_rejected(client, fake_mt):
    ids = seed(client)
    with SessionLocal() as db:
        video = db.get(Video, ids["video_id"])
        translation_service.create_translation_job(db, video, {"languages": ["ur"]})
    response = client.post(f"/api/v1/videos/{ids['video_id']}/translations/translate", json={"languages": ["ur"]})
    assert response.status_code == 409


def test_single_segment_engine_unavailable_returns_503(client, monkeypatch):
    def broken(*_args, **_kwargs):
        raise engine.TranslationUnavailable("model missing")

    monkeypatch.setattr(engine, "translate_texts", broken)
    ids = seed(client, [(None, "Hello.")])
    response = client.post(
        f"/api/v1/videos/{ids['video_id']}/translations/segments/{ids['segments'][0]}/translate", json={"languages": ["ur"]}
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "TRANSLATION_ENGINE_UNAVAILABLE"


def test_refiner_used_when_configured(client, fake_mt, monkeypatch):
    seen = {}

    def fake_refiner(prompt):
        seen["prompt"] = prompt
        return {"text": "سارہ، کدھر چلی؟", "roman": "Sara, kidhar chali?"}

    monkeypatch.setattr(translation_service.settings, "translation_refiner_url", "http://localhost:11434/v1")
    monkeypatch.setattr(engine, "refine_with_llm", fake_refiner)
    ids = seed(client)
    configure_scene(client, ids)
    result = client.post(
        f"/api/v1/videos/{ids['video_id']}/translations/segments/{ids['segments'][0]}/translate", json={"languages": ["ur"]}
    ).json()["translations"]["ur"]
    assert result["text"] == "سارہ، کدھر چلی؟"
    assert result["roman_text"] == "Sara, kidhar chali?"
    assert result["engine"].endswith("+llm")
    assert "Fixed spellings: Sara = سارہ" in seen["prompt"]
    assert "Register: informal" in seen["prompt"]
    assert "Next — Sara:" in seen["prompt"]


# ---------------------------------------------------------------------------
# Real model (opt-in)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.environ.get("RUN_REAL_MT") != "1", reason="Set RUN_REAL_MT=1 to run the real NLLB-200 model")
def test_real_nllb_urdu_and_hindi(client, monkeypatch):
    cache = os.environ.get("TRANSLATION_MODEL_CACHE_DIR")
    if cache:
        from pathlib import Path

        monkeypatch.setattr(engine.settings, "translation_model_cache_dir", Path(cache))
    monkeypatch.setattr(engine.settings, "translation_backend", "nllb")
    ids = seed(client)
    configure_scene(client, ids)
    job = translate_all(client, ids)
    if job["status"] != "completed":
        pytest.fail(f"real translation failed: {job['error_message']}")
    bundle = client.get(f"/api/v1/videos/{ids['video_id']}/translations").json()
    for item in bundle["lines"]:
        ur, hi = item["translations"]["ur"], item["translations"]["hi"]
        assert lang.URDU_RANGE.search(ur["text"]) and lang.DEVANAGARI_RANGE.search(hi["text"])
        assert not any(flag["severity"] == "error" for flag in ur["qc_flags"] + hi["qc_flags"])
        assert re.search(r"[a-z]", ur["roman_text"])
    first = line(bundle, ids["segments"][0])["translations"]
    assert "سارہ" in first["ur"]["text"] and "रही" in first["hi"]["text"]
    idiom = line(bundle, ids["segments"][3])["translations"]
    assert "بائیں ہاتھ کا کھیل" in idiom["ur"]["text"] and "बाएँ हाथ का खेल" in idiom["hi"]["text"]
