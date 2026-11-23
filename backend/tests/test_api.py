VIDEO_BYTES = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00video-bytes-for-tests"


def project_payload(name="Launch film"):
    return {
        "name": name,
        "description": "First dubbing project",
        "source_language": "en",
        "target_languages": ["ur", "hi"],
    }


def create_project(client, name="Launch film"):
    response = client.post("/api/v1/projects", json=project_payload(name))
    assert response.status_code == 201, response.text
    return response.json()


def test_health_and_info(client):
    health = client.get("/api/v1/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert health.json()["database"] == "ok"

    info = client.get("/api/v1/info")
    assert info.status_code == 200
    body = info.json()
    assert body["ai_stages"] == "planned"
    assert body["capabilities"]["video_upload"] is True
    assert body["capabilities"]["lip_sync"] is False
    assert body["max_upload_mb"] == 16
    assert body["upload_chunk_mb"] == 1
    assert body["capabilities"]["resumable_upload"] is True
    assert body["capabilities"]["frame_index"] is True
    assert body["capabilities"]["thumbnails"] is True


def test_project_crud(client):
    created = create_project(client)
    project_id = created["id"]
    assert created["status"] == "draft"
    assert created["videos"] == []

    listed = client.get("/api/v1/projects")
    assert listed.status_code == 200
    assert any(item["id"] == project_id for item in listed.json())

    searched = client.get("/api/v1/projects", params={"search": "launch"})
    assert [item["id"] for item in searched.json()] == [project_id]

    patched = client.patch(
        f"/api/v1/projects/{project_id}",
        json={"name": "Launch film v2", "status": "active"},
    )
    assert patched.status_code == 200
    assert patched.json()["name"] == "Launch film v2"
    assert patched.json()["status"] == "active"

    removed = client.delete(f"/api/v1/projects/{project_id}")
    assert removed.status_code == 204
    assert client.get(f"/api/v1/projects/{project_id}").status_code == 404


def test_error_envelope(client):
    response = client.get("/api/v1/projects/does-not-exist")
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "NOT_FOUND"
    assert "request_id" in body["error"]


def test_upload_validation(client):
    project = create_project(client)

    empty = client.post(
        f"/api/v1/uploads/projects/{project['id']}/videos",
        files={"file": ("empty.mp4", b"", "video/mp4")},
    )
    assert empty.status_code == 422
    assert empty.json()["error"]["code"] == "EMPTY_FILE"

    invalid = client.post(
        f"/api/v1/uploads/projects/{project['id']}/videos",
        files={"file": ("notes.txt", b"not a video", "video/mp4")},
    )
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "UNSUPPORTED_VIDEO"

    bad_type = client.post(
        f"/api/v1/uploads/projects/{project['id']}/videos",
        files={"file": ("sample.mp4", VIDEO_BYTES, "text/plain")},
    )
    assert bad_type.status_code == 422
    assert bad_type.json()["error"]["code"] == "UNSUPPORTED_MEDIA_TYPE"

    missing_project = client.post(
        "/api/v1/uploads/projects/missing/videos",
        files={"file": ("sample.mp4", VIDEO_BYTES, "video/mp4")},
    )
    assert missing_project.status_code == 404


def test_upload_metadata_stream_and_cleanup(client):
    project = create_project(client)

    response = client.post(
        f"/api/v1/uploads/projects/{project['id']}/videos",
        files={"file": ("sample.mp4", VIDEO_BYTES, "video/mp4")},
        data={"duration_ms": "1200", "width": "640", "height": "360"},
    )
    assert response.status_code == 201, response.text
    accepted = response.json()
    video = accepted["video"]
    job = accepted["job"]

    assert video["original_filename"] == "sample.mp4"
    assert video["size_bytes"] == len(VIDEO_BYTES)
    assert len(video["checksum_sha256"]) == 64
    assert video["duration_ms"] == 1200
    assert video["width"] == 640
    assert video["height"] == 360
    assert video["stream_url"] == f"/api/v1/videos/{video['id']}/stream"

    assert job["type"] == "media_import"
    assert len(job["steps"]) == 6
    assert [step["name"] for step in job["steps"]] == [
        "validate_file",
        "compute_checksum",
        "probe_metadata",
        "build_frame_index",
        "create_thumbnails",
        "finalize",
    ]

    job_response = client.get(f"/api/v1/jobs/{job['id']}")
    assert job_response.status_code == 200
    assert job_response.json()["status"] in {"queued", "running", "completed"}

    jobs_response = client.get(f"/api/v1/projects/{project['id']}/jobs")
    assert [item["id"] for item in jobs_response.json()] == [job["id"]]

    full = client.get(video["stream_url"])
    assert full.status_code == 200
    assert full.content == VIDEO_BYTES

    ranged = client.get(video["stream_url"], headers={"Range": "bytes=0-3"})
    assert ranged.status_code == 206
    assert ranged.content == VIDEO_BYTES[:4]
    assert ranged.headers["content-range"] == f"bytes 0-3/{len(VIDEO_BYTES)}"
    assert ranged.headers["accept-ranges"] == "bytes"

    bad_range = client.get(video["stream_url"], headers={"Range": "bytes=999-1000"})
    assert bad_range.status_code == 416
    assert bad_range.json()["error"]["code"] == "RANGE_NOT_SATISFIABLE"

    detail = client.get(f"/api/v1/videos/{video['id']}")
    assert detail.status_code == 200
    assert detail.json()["id"] == video["id"]

    removed = client.delete(f"/api/v1/videos/{video['id']}")
    assert removed.status_code == 204
    assert client.get(f"/api/v1/videos/{video['id']}").status_code == 404
    assert client.get(video["stream_url"]).status_code == 404
