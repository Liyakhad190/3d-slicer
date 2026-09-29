"""Boot the API and frontend together, then drive the real product flow.

This is the human-facing verification: a real HTTP server (not TestClient), a
real account, a real upload, a real CuraEngine slice, and the files the
frontend would fetch.

Run: python scripts/verify_stack.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
FIXTURES = BACKEND / "tests" / "fixtures"
API = "http://127.0.0.1:8123"
PASSWORD = "correct-horse-battery-staple"

sys.path.insert(0, str(BACKEND))


def line(label: str, value: object) -> None:
    print(f"  {label:<32} {value}")


def call(
    method: str, path: str, token: str | None = None, body: dict | None = None,
    files: tuple[str, bytes] | None = None,
) -> tuple[int, object]:
    url = f"{API}{path}"
    headers: dict[str, str] = {}
    data: bytes | None = None

    if token:
        headers["Authorization"] = f"Bearer {token}"

    if files is not None:
        name, payload = files
        boundary = f"----sp{uuid.uuid4().hex}"
        parts = [
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'.encode(),
            b"Content-Type: application/octet-stream\r\n\r\n",
            payload,
            f"\r\n--{boundary}--\r\n".encode(),
        ]
        data = b"".join(parts)
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            raw = response.read()
            ctype = response.headers.get("content-type", "")
            if "json" in ctype:
                return response.status, json.loads(raw)
            return response.status, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw[:400]


def main() -> int:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(BACKEND)
    env["ENVIRONMENT"] = "test"
    env["DEBUG"] = "false"
    env["DATA_DIR"] = str(REPO / "data" / "verify")
    env["AUTH_SECRET"] = "verification-secret-that-is-long-enough-1234"
    env["RATE_LIMIT_ENABLED"] = "false"
    env["LOG_LEVEL"] = "WARNING"

    print("starting API on :8123 …")
    # Log to a file, not a pipe: a PIPE nobody drains fills its buffer and
    # blocks the server, which looks exactly like the API hanging.
    log_path = Path(os.environ.get("TEMP", ".")) / "verify-stack-server.log"
    log_handle = log_path.open("w", encoding="utf-8", errors="replace")
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", "8123", "--log-level", "warning"],
        cwd=BACKEND, env=env, stdout=log_handle, stderr=log_handle,
    )

    try:
        for _ in range(60):
            if server.poll() is not None:
                print("API exited:", log_path.read_text(encoding="utf-8")[:1200])
                return 1
            try:
                status, _ = call("GET", "/health")
                if status == 200:
                    break
            except Exception:
                time.sleep(0.5)
        else:
            print("API did not become healthy in time")
            return 1

        line("health", "ok")

        status, ready = call("GET", "/ready")
        line("readiness", f"{ready.get('status')}  slicer_ok={ready.get('checks', {}).get('slicer', {}).get('ok')}")

        email = f"verify-{uuid.uuid4().hex[:8]}@example.com"
        status, reg = call("POST", "/api/v1/auth/register",
                           body={"email": email, "password": PASSWORD, "display_name": "Verify"})
        if status != 201:
            print("register failed:", reg)
            return 1
        token = reg["access_token"]
        line("registered", email)

        status, engines = call("GET", "/api/v1/slicers", token)
        if status == 200 and engines:
            engine = engines[0]
            line("slicer", f"{engine['display_name']} {engine['version']} [{engine['license']}]")
            line("  available", engine["available"])
            line("  licence review required", engine["capabilities"].get("licence_review_required"))

        # Promote a machine profile the way an operator would, then register.
        from app.db.session import session_scope
        from app.models.catalog import Machine, MachineProfile, ValidationStatus
        from app.db.base import utcnow
        from app.models.catalog import MaterialProfile

        with session_scope() as db:
            machine = db.query(Machine).filter(Machine.model == "Ender 3").one()
            profile = (db.query(MachineProfile)
                       .filter(MachineProfile.machine_id == machine.id)
                       .order_by(MachineProfile.profile_version.desc()).first())
            profile.validation_status = ValidationStatus.validated
            profile.validated_at = utcnow()
            profile.validated_by = "verify-stack"
            material = db.query(MaterialProfile).filter_by(brand_sku="pla-generic").first()
            payload = {
                "profile_id": str(profile.id),
                "material_id": str(material.id),
            }
        line("machine profile", "promoted to validated")

        status, printer = call("POST", "/api/v1/printers", token, {
            "name": "Verify printer",
            "machine_profile_id": payload["profile_id"],
            "nozzle_diameter_mm": 0.4,
        })
        if status != 201:
            print("printer failed:", printer)
            return 1
        line("printer registered", printer["name"])

        model_bytes = (FIXTURES / "cube_20mm.stl").read_bytes()
        status, upload = call("POST", "/api/v1/models", token,
                              files=("verify_cube.stl", model_bytes))
        if status != 201:
            print("upload failed:", upload)
            return 1
        line("uploaded", f"{upload['version']['format']}  {upload['version']['byte_size']} bytes")

        version_id = upload["version"]["id"]
        status, analysis = call("POST", f"/api/v1/models/versions/{version_id}/analyze", token)
        if status == 200:
            metrics = analysis["metrics"]
            line("analysis score", f"{analysis['printability_score']}/100  risk={analysis['risk_level']}")
            line("  dimensions", metrics["dimensions_mm"])
            line("  volume mm3", metrics["volume_mm3"])
            line("  min wall mm", metrics["estimated_min_wall_mm"])
            line("  findings", [f["code"] for f in analysis["findings"]] or "none")

        print("\nrunning the real slice …")
        started = time.perf_counter()
        status, job = call("POST", "/api/v1/slice", token, {
            "model_version_id": version_id,
            "printer_id": printer["id"],
            "material_profile_id": payload["material_id"],
            "objective": "balanced",
        })
        elapsed = (time.perf_counter() - started) * 1000

        if status != 201:
            print("slice failed:", job)
            return 1
        line("job status", job["status"])
        line("total wall time", f"{elapsed:.0f} ms")
        job_id = job["id"]

        status, stages = call("GET", f"/api/v1/slice/{job_id}/stages", token)
        for stage in stages:
            line(f"  stage {stage['stage']}", f"{stage['status']}  {stage['duration_ms']} ms")

        status, validation = call("GET", f"/api/v1/slice/{job_id}/validation", token)
        line("validation", validation.get("summary", validation.get("reason")))

        status, files = call("GET", f"/api/v1/slice/{job_id}/files", token)
        for entry in files:
            line("generated file", f"{entry['filename']}  {entry['byte_size']} bytes  blocked={entry['is_blocked']}")

        if files and not files[0]["is_blocked"]:
            status, gcode = call("GET", f"/api/v1/slice/{job_id}/files/{files[0]['id']}/download", token)
            if isinstance(gcode, bytes):
                text = gcode.decode("utf-8", errors="replace")
                layers = text.count(";LAYER:")
                line("downloaded gcode", f"{len(gcode):,} bytes, {layers} layers")
                line("  flavour", text.splitlines()[0] if text else "")
                line("  has M104 heat-up", "M104 S" in text)
                line("  has M140 bed heat", "M140 S" in text)

        status, path = call("GET", f"/api/v1/slice/{job_id}/toolpath", token)
        if status == 200:
            line("toolpath for viewer", f"{path['layer_count']} layers, {len(path['segments'])} segments")
            line("  stats", path["stats"])

        status, events = call("GET", f"/api/v1/slice/{job_id}/events", token)
        print("\npipeline log:")
        for event in events:
            line(f"  [{event['stage'] or '-'}]", event["message"])

        print("\nfrontend build:")
        dist = REPO / "frontend" / "dist"
        if dist.is_dir():
            line("  dist", f"built, {sum(f.stat().st_size for f in dist.rglob('*') if f.is_file()):,} bytes")
        else:
            line("  dist", "NOT BUILT")

        return 0
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            server.kill()
        log_handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
