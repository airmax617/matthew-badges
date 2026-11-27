"""Routes for the image review queue (/review) and the video badge scanner (/video).

Hooked into server.py's BadgeHandler: handle_get / handle_post return True when they handled the request.
All ids that touch the filesystem are checked against strict patterns (no path traversal).
"""
import base64, csv, io, json, os, re, shutil, sys, threading, time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE_DIR)
sys.path.insert(0, os.path.join(ROOT, "tools"))

QUEUE = os.path.join(ROOT, "state", "image-review")
AGENT_STATE = os.path.join(ROOT, "state", "image-agent.json")
TILES = os.path.join(ROOT, "public", "media", "tiles")
JOBS = os.path.join(ROOT, "state", "videos")

BADGE_ID = re.compile(r"[0-9a-f]{8}")
JOB_ID = re.compile(r"\d{8}-\d{6}")
CROP = re.compile(r"\d{3}\.jpg")
MAX_UPLOAD = 4 * 1024 ** 3
_state_lock = threading.Lock()


def _load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _save(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def _file(h, path, mime="image/jpeg", download=None):
    if not os.path.isfile(path):
        h.send_json({"error": "Not found"}, 404)
        return
    with open(path, "rb") as f:
        data = f.read()
    h.send_response(200)
    h.send_header("Content-Type", mime)
    h.send_header("Content-Length", len(data))
    h.send_header("Cache-Control", "no-store")
    if download:
        h.send_header("Content-Disposition", f'attachment; filename="{download}"')
    h.end_headers()
    h.wfile.write(data)


# ── image review queue ────────────────────────────────────────────────────────

def review_list():
    items = []
    if os.path.isdir(QUEUE):
        for d in sorted(os.listdir(QUEUE)):
            meta = _load(os.path.join(QUEUE, d, "meta.json"), None)
            if BADGE_ID.fullmatch(d) and meta:
                items.append(meta)
    state = _load(AGENT_STATE, {})
    reshoot = [{"id": k, "tip": v.get("reshoot_tip", ""), "seen": v.get("seen", "")}
               for k, v in state.items() if v.get("status") == "reshoot"]
    checked = sum(1 for v in state.values() if v.get("checked"))
    return {"items": items, "reshoot": reshoot, "checked": checked}


def review_decide(badge_id, approve, log=print):
    qdir = os.path.join(QUEUE, badge_id)
    meta = _load(os.path.join(qdir, "meta.json"), None)
    if not meta:
        return {"error": "Not in the queue"}, 404
    if approve:
        import publish
        dst = os.path.join(TILES, f"{badge_id}.jpg")
        shutil.copy(os.path.join(qdir, "candidate.jpg"), dst)
        if not publish.commit_and_push([dst], f"Better tile: {meta['name']} ({meta['method']})", log):
            return {"error": "Saved, but the push to GitHub failed; it will go out with the next publish"}, 500
    with _state_lock:
        state = _load(AGENT_STATE, {})
        e = state.setdefault(badge_id, {})
        e.update(status="ok" if approve else "rejected", decided=time.strftime("%Y-%m-%dT%H:%M:%S"))
        if approve:
            e["score"] = meta.get("score_after")
        _save(AGENT_STATE, state)
    shutil.rmtree(qdir, ignore_errors=True)
    return {"ok": True}, 200


def review_redo(h, badge_id):
    """'Try again' with a note: mark it and start the agent on just this badge."""
    import subprocess
    qdir = os.path.join(QUEUE, badge_id)
    meta = _load(os.path.join(qdir, "meta.json"), None)
    if not meta:
        return {"error": "Not in the queue"}, 404
    length = int(h.headers.get("Content-Length") or 0)
    try:
        note = str(json.loads(h.rfile.read(min(length, 10000)) or b"{}").get("note", "")).strip()[:500]
    except ValueError:
        note = ""
    if not note:
        return {"error": "Write what to change first"}, 400
    if meta.get("redo") and not meta.get("redo_error"):
        return {"error": "Already working on it"}, 409
    meta.setdefault("requests", []).append({"note": note, "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
    meta.update(redo=note, redo_at=time.time())
    meta.pop("redo_error", None)
    _save(os.path.join(qdir, "meta.json"), meta)
    log = open(os.path.join(ROOT, "logs", "image-agent.log"), "a")
    subprocess.Popen(["/usr/bin/python3", os.path.join(ROOT, "tools", "image_agent.py"), "--only", badge_id,
                      "--no-telegram"], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    log.close()
    return {"ok": True}, 200


# ── video scanner ─────────────────────────────────────────────────────────────

def video_upload(h):
    import video_badges as vb
    length = int(h.headers.get("Content-Length") or 0)
    if length <= 0 or length > MAX_UPLOAD:
        return h.send_json({"error": "Video missing or too big (4 GB max)"}, 400)
    job, d, dest = vb.new_job(h.headers.get("X-Filename", ""))
    left = length
    with open(dest, "wb") as f:
        while left > 0:
            chunk = h.rfile.read(min(left, 1 << 20))
            if not chunk:
                break
            f.write(chunk)
            left -= len(chunk)
    if left:
        shutil.rmtree(d, ignore_errors=True)
        return h.send_json({"error": "Upload was cut off, please try again"}, 400)
    _save(os.path.join(d, "status.json"), {"state": "running", "message": "Uploaded, starting", "updated": time.time()})
    threading.Thread(target=vb.run, args=(d,), daemon=True).start()
    h.send_json({"ok": True, "job": job})


def video_jobs():
    out = []
    if os.path.isdir(JOBS):
        for j in sorted(os.listdir(JOBS), reverse=True):
            if JOB_ID.fullmatch(j):
                st = _load(os.path.join(JOBS, j, "status.json"), {})
                out.append({"job": j, **st})
    return out


def video_add(job, n, server):
    """Identify a video crop at full quality and add it to the collection (auto-pushed like any scan)."""
    d = os.path.join(JOBS, job)
    res = _load(os.path.join(d, "result.json"), None)
    b = next((x for x in (res or {}).get("badges", []) if x["n"] == n), None)
    if not b or not b.get("crop"):
        return {"error": "Badge not found"}, 404
    if b.get("added_id"):
        return {"ok": True, "entry": {"id": b["added_id"]}, "already": True}, 200
    with open(os.path.join(d, "crops", b["crop"]), "rb") as f:
        img_b64 = base64.b64encode(f.read()).decode()
    info = server.identify_badge(img_b64, "image/jpeg")
    if info.get("error"):
        return {"error": info["error"]}, 502
    filename = server.save_photos(img_b64, "image/jpeg")
    import uuid
    from datetime import datetime
    coll = server.load_collection()
    entry = {"id": uuid.uuid4().hex[:8], "added": datetime.now().isoformat(),
             "photo": f"/photos/{filename}", "source": f"video {job}", **info}
    coll["badges"].append(entry)
    server.save_collection(coll, badge_name=info.get("badge_name"))
    b["added_id"] = entry["id"]
    _save(os.path.join(d, "result.json"), res)
    return {"ok": True, "entry": entry}, 200


def video_csv(job):
    res = _load(os.path.join(JOBS, job, "result.json"), {})
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["#", "Make", "Badge", "Look", "Status", "Vault IDs", "Seen at (s)", "Note"])
    for b in res.get("badges", []):
        w.writerow([b["n"] + 1, b["make"], b["badge_name"], b["look"], b["status"].replace("_", " "),
                    " ".join(b["vault_ids"]), b["t"], b["note"]])
    return buf.getvalue().encode()


# ── routing ───────────────────────────────────────────────────────────────────

def handle_get(h, path):
    if path in ("/review", "/review.html"):
        _file(h, os.path.join(BASE_DIR, "review.html"), "text/html; charset=utf-8")
    elif path == "/api/review":
        h.send_json(review_list())
    elif re.fullmatch(r"/review-img/[0-9a-f]{8}/(current|candidate)\.jpg", path):
        _, _, bid, name = path.split("/")
        _file(h, os.path.join(QUEUE, bid, name))
    elif re.fullmatch(r"/tile/[0-9a-f]{8}\.jpg", path):
        _file(h, os.path.join(TILES, path.split("/")[-1]))
    elif path in ("/video", "/video.html"):
        _file(h, os.path.join(BASE_DIR, "video.html"), "text/html; charset=utf-8")
    elif path == "/api/videos":
        h.send_json(video_jobs())
    elif re.fullmatch(r"/api/video/\d{8}-\d{6}", path):
        job = path.split("/")[-1]
        d = os.path.join(JOBS, job)
        h.send_json({"job": job, "status": _load(os.path.join(d, "status.json"), {}),
                     "result": _load(os.path.join(d, "result.json"), None)})
    elif re.fullmatch(r"/api/video/\d{8}-\d{6}/csv", path):
        job = path.split("/")[-2]
        data = video_csv(job)
        h.send_response(200)
        h.send_header("Content-Type", "text/csv; charset=utf-8")
        h.send_header("Content-Disposition", f'attachment; filename="badges-{job}.csv"')
        h.send_header("Content-Length", len(data))
        h.end_headers()
        h.wfile.write(data)
    elif re.fullmatch(r"/video-crop/\d{8}-\d{6}/\d{3}\.jpg", path):
        _, _, job, name = path.split("/")
        _file(h, os.path.join(JOBS, job, "crops", name))
    else:
        return False
    return True


def handle_post(h, path, server):
    m = re.fullmatch(r"/api/review/([0-9a-f]{8})/redo", path)
    if m:
        body, code = review_redo(h, m.group(1))
        h.send_json(body, code)
        return True
    m = re.fullmatch(r"/api/review/([0-9a-f]{8})/(approve|reject)", path)
    if m:
        body, code = review_decide(m.group(1), m.group(2) == "approve")
        h.send_json(body, code)
        return True
    if path == "/api/video/upload":
        video_upload(h)
        return True
    m = re.fullmatch(r"/api/video/(\d{8}-\d{6})/add/(\d{1,3})", path)
    if m:
        body, code = video_add(m.group(1), int(m.group(2)), server)
        h.send_json(body, code)
        return True
    return False
