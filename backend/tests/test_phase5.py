from app.services.mixer import MIX_TRACK_ROLES, default_settings


PLANNED_ROLES = {"urdu_dub", "hindi_dub"}


def create_project(client, name="Phase 5 film"):
    response = client.post(
        "/api/v1/projects",
        json={"name": name, "source_language": "en", "target_languages": ["ur", "hi"]},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_info_reports_phase5_mixer_capabilities(client):
    body = client.get("/api/v1/info").json()
    assert body["version"] == "0.5.0-phase8"
    assert body["ai_stages"] == "partial"
    for flag in (
        "audio_extraction",
        "audio_separation",
        "audio_preview",
        "audio_mixer",
        "dialogue_control",
        "synchronized_mix_preview",
        "project_mix_settings",
    ):
        assert body["capabilities"][flag] is True
    for flag in ("voice_generation", "lip_sync", "timeline_editor"):
        assert body["capabilities"][flag] is False


def test_default_mixer_settings_cover_six_tracks(client):
    project = create_project(client)
    body = client.get(f"/api/v1/projects/{project['id']}/audio-mix").json()

    assert body["project_id"] == project["id"]
    assert body["master_volume"] == 1.0
    assert body["preview_enabled"] is False
    assert set(body["tracks"]) == set(MIX_TRACK_ROLES)
    assert body["tracks"]["dialogue"]["label"] == "Original Dialogue"
    assert body["tracks"]["dialogue"]["volume"] == 0.9
    assert body["tracks"]["music"]["volume"] == 0.8
    assert body["tracks"]["sfx"]["volume"] == 0.8
    assert body["tracks"]["ambience"]["volume"] == 0.55
    for role in MIX_TRACK_ROLES:
        track = body["tracks"][role]
        assert track["muted"] is False
        assert track["solo"] is False
        assert track["enabled"] is (role not in PLANNED_ROLES)
    assert body["updated_at"] is not None


def test_mixer_settings_persist_reopen_reset_and_validate(client):
    project = create_project(client)
    project_id = project["id"]

    payload = {
        "master_volume": 0.75,
        "preview_enabled": True,
        "tracks": {
            "dialogue": {"volume": 0.35, "muted": True, "solo": False, "enabled": True},
            "music": {"volume": 0.2, "muted": False, "solo": True, "enabled": True},
            "sfx": {"volume": 0.65, "muted": False, "solo": False, "enabled": False},
            "ambience": {"volume": 0.1, "muted": True, "solo": False, "enabled": True},
            "urdu_dub": {"volume": 0.5, "muted": False, "solo": False, "enabled": True},
            "hindi_dub": {"volume": 0.6, "muted": True, "solo": False, "enabled": False},
        },
    }
    saved = client.put(f"/api/v1/projects/{project_id}/audio-mix", json=payload)
    assert saved.status_code == 200, saved.text
    saved_body = saved.json()
    assert saved_body["master_volume"] == 0.75
    assert saved_body["preview_enabled"] is True

    reopened = client.get(f"/api/v1/projects/{project_id}/audio-mix").json()
    assert reopened["master_volume"] == 0.75
    assert reopened["preview_enabled"] is True
    assert reopened["tracks"] == saved_body["tracks"]

    reset = client.post(f"/api/v1/projects/{project_id}/audio-mix/reset")
    assert reset.status_code == 200, reset.text
    reset_body = reset.json()
    assert reset_body["master_volume"] == 1.0
    assert reset_body["preview_enabled"] is False
    for role in MIX_TRACK_ROLES:
        expected = default_settings()["tracks"][role]
        assert reset_body["tracks"][role]["volume"] == expected["volume"]
        assert reset_body["tracks"][role]["muted"] is False
        assert reset_body["tracks"][role]["solo"] is False
        assert reset_body["tracks"][role]["enabled"] == expected["enabled"]

    bad_role = client.put(
        f"/api/v1/projects/{project_id}/audio-mix",
        json={"tracks": {"unknown_dub": {"volume": 0.5}}},
    )
    assert bad_role.status_code == 422
    assert bad_role.json()["error"]["code"] == "INVALID_MIX_TRACK"

    bad_volume = client.put(
        f"/api/v1/projects/{project_id}/audio-mix",
        json={"tracks": {"dialogue": {"volume": 1.5}}},
    )
    assert bad_volume.status_code in (400, 422)
    assert bad_volume.json()["error"]["code"] in ("VALIDATION_ERROR", "INVALID_MIX_SETTINGS")

    missing = client.get("/api/v1/projects/does-not-exist/audio-mix")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "NOT_FOUND"


def test_mixer_settings_are_isolated_per_project(client):
    first = create_project(client, "Mixer A")
    second = create_project(client, "Mixer B")

    changed = client.put(
        f"/api/v1/projects/{first['id']}/audio-mix",
        json={"tracks": {"dialogue": {"volume": 0.15, "muted": True}}},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["tracks"]["dialogue"]["volume"] == 0.15
    assert changed.json()["tracks"]["dialogue"]["muted"] is True

    untouched = client.get(f"/api/v1/projects/{second['id']}/audio-mix").json()
    assert untouched["tracks"]["dialogue"]["volume"] == 0.9
    assert untouched["tracks"]["dialogue"]["muted"] is False
