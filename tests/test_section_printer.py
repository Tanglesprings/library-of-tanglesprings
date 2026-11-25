import base64
import hashlib
import json
from uuid import uuid4

from app.database import set_setting
from app.services.section_printing import WORKER_DIGEST


def test_section_job_prints_once_and_accepts_a_repeated_cups_receipt(admin_client, client, db):
    key = "a" * 43
    set_setting(db, WORKER_DIGEST, hashlib.sha256(key.encode()).hexdigest())
    db.commit()
    job_id = str(uuid4())
    form = {"job_id": job_id, "labels": json.dumps(["Mystery", "Sci-Fi & Fantasy"])}
    for _ in range(2):
        result = admin_client.post("/labels/sections/jobs", data=form, follow_redirects=False)
        assert result.status_code == 303
        assert result.headers["location"] == f"/labels/sections/jobs/{job_id}"

    claim = {"type": "claim", "queue": "DYMO_LabelWriter_450_Turbo", "widthMm": 27.686, "heightMm": 88.9}
    assert client.post("/api/label-printer", json=claim).status_code == 401
    auth = {"Authorization": f"Bearer {key}"}
    job = client.post("/api/label-printer", json=claim, headers=auth)
    assert job.status_code == 200 and job.json()["id"] == job_id
    assert base64.b64decode(job.json()["pdf"]).startswith(b"%PDF-")
    assert client.post("/api/label-printer", json=claim, headers=auth).status_code == 204

    receipt = {"type": "submitted", "id": job_id, "spoolId": "DYMO_LabelWriter_450_Turbo-42"}
    for _ in range(2):
        assert client.post("/api/label-printer", json=receipt, headers=auth).status_code == 204
    result = admin_client.get(f"/labels/sections/jobs/{job_id}")
    assert "2 section labels" in result.text and receipt["spoolId"] in result.text


def test_section_links_select_the_existing_destination(admin_client, db):
    section = db.execute("INSERT INTO locations (name, label) VALUES ('Mystery', 'Mystery')").lastrowid
    db.commit()
    assert f'data-move-to="{section}"' in admin_client.get(f"/browse?move_to={section}").text
    scan = admin_client.get(f"/scan?mode=move&move_to={section}").text
    assert 'data-seed-mode="move"' in scan and f'data-seed-location="{section}"' in scan
    assert 'checked' in admin_client.get(f"/labels/sections?location_id={section}").text


def test_section_preview_tracks_the_stock_reported_by_the_worker(admin_client, client, db):
    key = "b" * 43
    set_setting(db, WORKER_DIGEST, hashlib.sha256(key.encode()).hexdigest())
    db.commit()
    claim = {"type": "claim", "queue": "DYMO_LabelWriter_450_Turbo", "widthMm": 27.686, "heightMm": 88.9}
    auth = {"Authorization": f"Bearer {key}"}
    assert client.post("/api/label-printer", json=claim, headers=auth).status_code == 204
    page = admin_client.get("/labels/sections").text
    assert 'name="width" value="88.9"' in page
    assert 'name="height" value="27.686"' in page

    claim.update(widthMm=57.15, heightMm=31.75)
    assert client.post("/api/label-printer", json=claim, headers=auth).status_code == 204
    page = admin_client.get("/labels/sections").text
    assert 'name="width" value="57.15"' in page
    assert 'name="height" value="31.75"' in page
    preview = admin_client.post("/labels/sections", data={"section_names": "Mystery"}).text
    assert "size: 57.15mm 31.75mm" in preview
    assert "57.15 × 31.75 mm" in preview
