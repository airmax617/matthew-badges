#!/usr/bin/python3
"""Turn a video of Matthew's badges into a badge list.

  frames  - ffmpeg samples the video, keeps the sharpest frame in each slice and drops near-duplicates
  detect  - Claude lists every badge visible in each frame (make, name, look, box)
  merge   - Claude merges sightings of the same physical badge across frames and checks each
            against the vault (new / already in the vault / maybe)
  crops   - best sighting of each badge is cropped out as its picture

Writes <job>/status.json while running and <job>/result.json at the end.
Usage: video_badges.py VIDEO_OR_JOB_DIR
"""
import json, os, re, shutil, subprocess, sys, time, traceback
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from PIL import Image, ImageFilter, ImageStat

import claude_vision as cv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COLLECTION = os.path.join(ROOT, "state", "badge-collection.json")
JOBS = os.path.join(ROOT, "state", "videos")
FFMPEG, FFPROBE = "/opt/homebrew/bin/ffmpeg", "/opt/homebrew/bin/ffprobe"
MAX_FRAMES = 90
WORKERS = 4

DETECT_SCHEMA = {
    "type": "object",
    "properties": {"badges": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "make": {"type": "string"},
            "badge_name": {"type": "string", "description": "e.g. 'hood ornament', 'wheel center cap', 'MUSTANG script'"},
            "look": {"type": "string", "description": "short visual description: colors, finish, shape, damage"},
            "box": {"type": "array", "items": {"type": "integer"},
                    "description": "[x0, y0, x1, y1] on a 0-1000 scale of the frame"},
            "clear": {"type": "boolean", "description": "sharp and big enough to identify with confidence"},
        },
        "required": ["make", "badge_name", "look", "box", "clear"], "additionalProperties": False}}},
    "required": ["badges"], "additionalProperties": False,
}
DETECT_PROMPT = """This is one frame from a video a boy took of his car badge collection (emblems, hood ornaments,
wheel center caps, script nameplates, grilles). List every badge you can see in the frame, one entry per physical piece.
Read lettering exactly; use the logo shape and colors. Include partly visible pieces only if you can still tell what they
are. Use make "Unknown" if you can't tell. Box coordinates are on a 0-1000 scale (x left->right, y top->bottom) and should
hug the piece. If there are no badges, return an empty list."""

MERGE_SCHEMA = {
    "type": "object",
    "properties": {"badges": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "make": {"type": "string"},
            "badge_name": {"type": "string"},
            "look": {"type": "string"},
            "sightings": {"type": "array", "items": {"type": "string"}, "description": "detection ids"},
            "status": {"type": "string", "enum": ["new", "in_vault", "maybe_in_vault"]},
            "vault_ids": {"type": "array", "items": {"type": "string"}},
            "note": {"type": "string"},
        },
        "required": ["make", "badge_name", "look", "sightings", "status", "vault_ids", "note"],
        "additionalProperties": False}}},
    "required": ["badges"], "additionalProperties": False,
}
MERGE_PROMPT = """A video pans across a boy's car badge collection. Below are badge sightings detected frame by frame
(id, time in seconds, make, name, look, box on a 0-1000 scale, clear). The same physical badge shows up in several nearby
frames as the camera moves, usually drifting smoothly across the frame. Merge the sightings into the list of distinct
physical badges:
- Sightings of the same make/design at nearby times and consistent positions are one badge.
- Two pieces of the same design seen side by side in one frame, or with different looks/damage, are separate badges.
- When unsure whether two sightings are the same piece, merge them (better to undercount than double count).
- Drop sightings that are clearly not car badges.
Use the clearest sighting's reading for make and badge_name.

Then compare each badge with the vault (badges already catalogued, below). status "in_vault" when a vault entry is
clearly this badge (same make and same kind of piece and look), "maybe_in_vault" when a vault entry could be it,
"new" otherwise; list the matching vault ids. Note anything worth knowing in a few words (or leave it empty).

SIGHTINGS:
{sightings}

VAULT:
{vault}"""


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def sharpness(img):
    g = img.convert("L")
    g.thumbnail((640, 640))
    return ImageStat.Stat(g.filter(ImageFilter.FIND_EDGES)).var[0]


def dhash(img, size=8):
    g = img.convert("L").resize((size + 1, size), Image.LANCZOS)
    px = list(g.getdata())
    return sum(1 << i for i in range(size * size)
               if px[(i // size) * (size + 1) + i % size] > px[(i // size) * (size + 1) + i % size + 1])


def extract_frames(video, outdir, status):
    dur = float(subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", video],
                               capture_output=True, text=True, timeout=60).stdout.strip() or 0)
    if dur <= 0:
        raise RuntimeError("couldn't read the video (is it a video file?)")
    # ~3 samples per kept frame so we can pick the sharpest
    fps = min(4.0, max(0.5, 3 * MAX_FRAMES / dur))
    status(f"Pulling frames from a {dur:.0f}s video")
    os.makedirs(outdir, exist_ok=True)
    subprocess.run([FFMPEG, "-v", "error", "-i", video, "-vf", f"fps={fps:.3f},scale='min(1600,iw)':-2",
                    "-q:v", "3", os.path.join(outdir, "s%05d.jpg")], check=True, timeout=1800)
    samples = sorted(f for f in os.listdir(outdir) if f.startswith("s"))
    scored = []
    for i, f in enumerate(samples):
        with Image.open(os.path.join(outdir, f)) as im:
            scored.append((f, i / fps, sharpness(im), dhash(im)))
    # sharpest frame in each slice of the video
    n_keep = min(MAX_FRAMES, len(scored))
    per = max(1, len(scored) / n_keep)
    picks, i = [], 0.0
    while int(i) < len(scored):
        chunk = scored[int(i):int(i + per)] or scored[int(i):int(i) + 1]
        picks.append(max(chunk, key=lambda s: s[2]))
        i += per
    med = sorted(s[2] for s in picks)[len(picks) // 2] if picks else 0
    kept, last = [], None
    for f, t, sh, h in picks:
        if sh < 0.3 * med:
            continue  # motion blur
        if last is not None and bin(h ^ last).count("1") <= 4:
            continue  # camera didn't move
        kept.append({"file": f, "t": round(t, 1)})
        last = h
    for f in samples:
        if f not in {k["file"] for k in kept}:
            os.remove(os.path.join(outdir, f))
    return dur, kept


def detect(frames, fdir, status):
    done = [0]

    def one(fr):
        try:
            out = cv.ask_json([cv.image_block(os.path.join(fdir, fr["file"])),
                               {"type": "text", "text": DETECT_PROMPT}], DETECT_SCHEMA, effort="medium")
        except cv.ClaudeError as e:
            out = {"badges": [], "error": str(e)}
        done[0] += 1
        status(f"Looking for badges: frame {done[0]} of {len(frames)}")
        return out

    with ThreadPoolExecutor(WORKERS) as ex:
        results = list(ex.map(one, frames))
    sightings = []
    for fi, (fr, res) in enumerate(zip(frames, results)):
        for bi, b in enumerate(res["badges"]):
            if len(b["box"]) != 4:
                continue
            sightings.append({"id": f"f{fi}b{bi}", "frame": fi, "t": fr["t"], **b})
    return sightings


def merge(sightings, vault):
    lines = [f'{s["id"]} t={s["t"]} {s["make"]} | {s["badge_name"]} | {s["look"]} | box={s["box"]} | '
             f'{"clear" if s["clear"] else "unclear"}' for s in sightings]
    vlines = [f'{b["id"]}: {b.get("make", "")} | {b.get("badge_name", "")} | {(b.get("description") or "")[:90]}'
              for b in vault]
    prompt = MERGE_PROMPT.format(sightings="\n".join(lines), vault="\n".join(vlines))
    return cv.ask_json([{"type": "text", "text": prompt}], MERGE_SCHEMA, effort="high", max_tokens=64000)["badges"]


def crop(sighting, fdir, frames, out_path, margin=0.3):
    """Square crop around the detection box, padded generously (model boxes tend to run tight)."""
    with Image.open(os.path.join(fdir, frames[sighting["frame"]]["file"])) as im:
        W, H = im.size
        x0, y0, x1, y1 = [max(0, min(1000, v)) for v in sighting["box"]]
        x0, x1 = sorted((x0, x1)); y0, y1 = sorted((y0, y1))
        cx, cy = (x0 + x1) / 2 * W / 1000, (y0 + y1) / 2 * H / 1000
        half = max((x1 - x0) * W, (y1 - y0) * H) / 1000 * (1 + 2 * margin) / 2
        box = (int(cx - half), int(cy - half), int(cx + half), int(cy + half))
        box = (max(0, box[0]), max(0, box[1]), min(W, box[2]), min(H, box[3]))
        if box[2] - box[0] < 8 or box[3] - box[1] < 8:
            return False
        im.crop(box).convert("RGB").save(out_path, quality=88)
    return True


def run(job_dir):
    status_path = os.path.join(job_dir, "status.json")
    started = time.time()

    def status(msg, state="running"):
        write_json(status_path, {"state": state, "message": msg, "updated": time.time(), "started": started})

    try:
        video = next(os.path.join(job_dir, f) for f in os.listdir(job_dir) if f.startswith("input"))
        fdir, cdir = os.path.join(job_dir, "frames"), os.path.join(job_dir, "crops")
        dur, frames = extract_frames(video, fdir, status)
        if not frames:
            raise RuntimeError("no usable frames - was the video very blurry or dark?")
        sightings = detect(frames, fdir, status)
        status(f"Found {len(sightings)} sightings; sorting out which are the same badge")
        with open(COLLECTION) as f:
            coll = json.load(f)
        vault = coll.get("badges", [])
        badges = merge(sightings, vault) if sightings else []
        by_id = {s["id"]: s for s in sightings}
        os.makedirs(cdir, exist_ok=True)
        out = []
        for n, b in enumerate(badges):
            seen = [by_id[i] for i in b["sightings"] if i in by_id]
            if not seen:
                continue
            best = max(seen, key=lambda s: (s["clear"], (s["box"][2] - s["box"][0]) * (s["box"][3] - s["box"][1])))
            name = f"{n:03d}.jpg"
            has = crop(best, fdir, frames, os.path.join(cdir, name))
            out.append({**b, "n": n, "crop": name if has else None, "t": best["t"],
                        "clear": any(s["clear"] for s in seen),
                        "vault_ids": [v for v in b["vault_ids"] if any(x["id"] == v for x in vault)]})
        out.sort(key=lambda b: ({"new": 0, "maybe_in_vault": 1, "in_vault": 2}[b["status"]], b["make"]))
        result = {"duration": round(dur), "frames": len(frames), "sightings": len(sightings), "badges": out,
                  "counts": {k: sum(1 for b in out if b["status"] == k) for k in ("new", "maybe_in_vault", "in_vault")},
                  "finished": time.time()}
        write_json(os.path.join(job_dir, "result.json"), result)
        c = result["counts"]
        status(f"Done: {len(out)} badges ({c['new']} new, {c['maybe_in_vault']} maybe, {c['in_vault']} already in the vault)",
               "done")
        # frames are only needed for crops; keep the disk tidy
        shutil.rmtree(fdir, ignore_errors=True)
    except Exception as e:
        traceback.print_exc()
        status(f"Something went wrong: {e}", "error")


def new_job(src_name):
    os.makedirs(JOBS, exist_ok=True)
    job = time.strftime("%Y%m%d-%H%M%S")
    d = os.path.join(JOBS, job)
    os.makedirs(d)
    ext = os.path.splitext(src_name or "")[1].lower()
    ext = ext if re.fullmatch(r"\.[a-z0-9]{1,5}", ext) else ".mov"
    return job, d, os.path.join(d, "input" + ext)


if __name__ == "__main__":
    arg = sys.argv[1]
    if os.path.isdir(arg):
        run(arg)
    else:
        job, d, dest = new_job(arg)
        shutil.copy(arg, dest)
        print("job", job)
        run(d)
        print(open(os.path.join(d, "status.json")).read())
