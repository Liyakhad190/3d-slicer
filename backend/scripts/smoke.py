"""Manual end-to-end smoke check against a real in-process app.

Run: python scripts/smoke.py
Not a pytest module — this is the "does the whole thing actually work" script.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.main import create_app  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
PASSWORD = "correct-horse-battery-staple"


def line(label: str, value: object) -> None:
    print(f"  {label:<34} {value}")


def main() -> int:
    settings = get_settings()
    print(f"data_dir : {settings.data_dir}")
    print(f"database : {settings.database_url}")

    failures: list[str] = []

    with TestClient(create_app()) as client:
        ready = client.get("/ready").json()
        line("readiness", f"{ready['status']}  slicer_ok={ready['checks']['slicer']['ok']}")

        reg = client.post(
            "/api/v1/auth/register",
            json={"email": "smoke@example.com", "password": PASSWORD, "display_name": "Smoke"},
        )
        line("register", reg.status_code)
        if reg.status_code != 201:
            print(reg.json())
            return 1
        headers = {"Authorization": f"Bearer {reg.json()['access_token']}"}

        for path in (
            "/api/v1/machines",
            "/api/v1/machine-profiles",
            "/api/v1/materials",
            "/api/v1/material-profiles",
            "/api/v1/nozzles",
            "/api/v1/printers",
            "/api/v1/models",
            "/api/v1/models/formats",
        ):
            response = client.get(path, headers=headers)
            body = response.json()
            count = len(body) if isinstance(body, list) else "-"
            line(path, f"{response.status_code}  n={count}")
            if response.status_code != 200:
                failures.append(path)

        # --- upload + analyse --------------------------------------------
        data = (FIXTURES / "thin_wall.stl").read_bytes()
        upload = client.post(
            "/api/v1/models",
            headers=headers,
            files={"file": ("robot_part.stl", data, "application/octet-stream")},
        )
        line("upload STL", upload.status_code)
        if upload.status_code != 201:
            print(upload.json())
            return 1
        version_id = upload.json()["version"]["id"]

        again = client.post(
            "/api/v1/models",
            headers=headers,
            files={"file": ("robot_part.stl", data, "application/octet-stream")},
        )
        line("re-upload deduplicated", again.json()["deduplicated"])
        if not again.json()["deduplicated"]:
            failures.append("dedup")

        analysis = client.post(f"/api/v1/models/versions/{version_id}/analyze", headers=headers)
        line("analyze", analysis.status_code)
        if analysis.status_code == 200:
            payload = analysis.json()
            metrics = payload["metrics"]
            line("  score / risk", f"{payload['printability_score']} / {payload['risk_level']}")
            line("  dimensions", metrics["dimensions_mm"])
            line("  volume mm3", metrics["volume_mm3"])
            line("  min wall mm", metrics["estimated_min_wall_mm"])
            for finding in payload["findings"]:
                line(f"  [{finding['severity']}]", finding["message"][:78])
        else:
            failures.append("analyze")

        download = client.get(f"/api/v1/models/versions/{version_id}/file", headers=headers)
        line("download original", f"{download.status_code} {len(download.content)} bytes")

        # --- security checks ----------------------------------------------
        exe = client.post(
            "/api/v1/models",
            headers=headers,
            files={"file": ("evil.stl", b"MZ\x90\x00" * 200, "application/octet-stream")},
        )
        line("PE binary named .stl", f"{exe.status_code} {exe.json()['error']['code']}")
        if exe.status_code != 415:
            failures.append("content-sniffing")

        traversal = client.post(
            "/api/v1/models",
            headers=headers,
            files={"file": ("../../etc/passwd.stl", data, "application/octet-stream")},
        )
        stored = traversal.json()["version"] if traversal.status_code < 300 else {}
        line("traversal filename stored", stored.get("original_filename", "-"))
        line("  sanitized", stored.get("original_filename", "-").replace("/", "|"))

        unauth = client.get("/api/v1/models")
        line("unauthenticated read", unauth.status_code)
        if unauth.status_code != 401:
            failures.append("auth")

        bad_login = client.post(
            "/api/v1/auth/login", json={"email": "smoke@example.com", "password": "wrong-password-here"}
        )
        line("wrong password", bad_login.status_code)
        if bad_login.status_code != 401:
            failures.append("login")

    print()
    if failures:
        print(f"FAILED: {failures}")
        return 1
    print("SMOKE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
