#!/usr/bin/python3
"""Nightly image agent: keeps the badge tiles on matthewspivak.com looking clean.

Each run:
  1. Any badge without a tile gets one (rendered from its original photo) and is published right away.
  2. Up to --limit tiles are reviewed by Claude (straight? clean cut-out? sharp?).
  3. For weak tiles it renders alternatives (rotation, other cut-out models) and asks Claude to pick the best.
  4. A clearly better version goes into the review queue (state/image-review/<id>/) for Max to approve at
     http://<mac-mini>:8282/review. Nothing replaces a live image without that approval.
  5. Photos no fix can save (blurry, a hand in the shot) go on the "please re-photograph" list.
  6. Telegram summary if anything needs attention.
  On the review page Max can also press "Try again" with a note ("turn it a bit more left", "the bottom is cut off").
  That badge is re-done from the note (right away, and again each night until it has a new proposal): Claude reads the
  note plus the photo/tile/last proposal, picks an edit recipe (crop, turn, cut-out model, size, brightness), and the
  result goes back in the queue with a one-line reply.

Usage: image_agent.py [--limit 10] [--only ID ...] [--no-telegram] [--dry-run]
"""
import argparse, json, os, shutil, sys, time, traceback
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from PIL import Image, ImageEnhance, ImageOps

import claude_vision as cv
import render_badge as rb
import publish

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COLLECTION = os.path.join(ROOT, "state", "badge-collection.json")
TILES = os.path.join(ROOT, "public", "media", "tiles")
ORIGINALS = os.path.join(ROOT, "app", "photos_original")
STATE = os.path.join(ROOT, "state", "image-agent.json")
QUEUE = os.path.join(ROOT, "state", "image-review")
RECHECK_DAYS = 60      # re-look at tiles that passed, in case the bar or the tools improved
RETRY_REJECTED_DAYS = 90
MODELS = ["isnet-general-use", "u2net"]

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "what_i_see": {"type": "string"},
        "score": {"type": "integer", "description": "1-10, 10 = catalogue-quality tile"},
        "problems": {"type": "array", "items": {"type": "string", "enum": [
            "rotated", "leftover_background", "parts_cut_off", "jagged_edges", "blurry", "too_dark",
            "hand_or_fingers", "multiple_objects", "too_small", "other"]}},
        "rotate_clockwise_degrees": {"type": "integer",
                                     "description": "-180..180; how far to turn it so it sits upright as mounted on a car"},
        "photo_unfixable": {"type": "boolean",
                            "description": "true if only a new photo would fix it (out of focus, hand covering it)"},
        "reshoot_tip": {"type": "string"},
    },
    "required": ["what_i_see", "score", "problems", "rotate_clockwise_degrees", "photo_unfixable", "reshoot_tip"],
    "additionalProperties": False,
}
REVIEW_PROMPT = """This is a product-style tile for one car badge on a boy's collection website: the badge is cut out of
its photo and centered on a dark background. Badge: {name}.

Judge it the way a catalogue photo editor would:
- Is it upright, the way it sits on the car (lettering horizontal and readable, logo the right way up)? Roundels with
  lettering around the rim count as upright when the logo in the middle is upright. If it should be turned, give the
  clockwise degrees (negative = counter-clockwise); otherwise 0.
- Is the cut-out clean: no leftover table, cloth or wall, no hand, nothing chopped off, smooth edges?
- Is the badge sharp and well lit?
Score 1-10 (8+ means leave it alone). Set photo_unfixable only when the source photo itself is the problem
(out of focus, motion blur, fingers covering it), and then say in reshoot_tip how to retake it, in one short sentence."""

COMPARE_SCHEMA = {
    "type": "object",
    "properties": {
        "notes": {"type": "string"},
        "scores": {"type": "array", "items": {"type": "integer"},
                   "description": "1-10 per image, same order as shown"},
        "best": {"type": "integer", "description": "index of the best image (0 = current)"},
    },
    "required": ["notes", "scores", "best"],
    "additionalProperties": False,
}
COMPARE_PROMPT = """Below are {n} versions of the same car badge tile ({name}). Image 0 is the one live on the site now;
the others are alternatives. Score each 1-10 on: upright like it sits on a car, clean cut-out (no leftover background,
nothing chopped off, smooth edges), the badge looking complete and true to the real part. Then pick the best one.
Pick 0 unless another is clearly better - a change has to be worth it."""


EDIT_SCHEMA = {
    "type": "object",
    "properties": {
        "plan": {"type": "string"},
        "reply": {"type": "string", "description": "one short sentence to Max: what you changed"},
        "start_from": {"type": "string", "enum": ["original_photo", "live_tile", "last_proposal"]},
        "crop": {"type": "object", "description": "box on the start image, 0-1000 scale; 0,0,1000,1000 = no crop",
                 "properties": {k: {"type": "integer"} for k in ("x0", "y0", "x1", "y1")},
                 "required": ["x0", "y0", "x1", "y1"], "additionalProperties": False},
        "rotate_clockwise_degrees": {"type": "integer", "description": "-180..180, applied after the crop"},
        "cut_out": {"type": "boolean", "description": "remove the background again (false = keep the pixels as they are)"},
        "models": {"type": "array", "items": {"type": "string", "enum": MODELS},
                   "description": "cut-out models to try; empty = all"},
        "fill": {"type": "number", "description": "how much of the tile the badge fills, 0.5-0.95 (now 0.80)"},
        "brightness": {"type": "number", "description": "1.0 = unchanged, 0.6-1.6"},
        "contrast": {"type": "number", "description": "1.0 = unchanged, 0.6-1.6"},
    },
    "required": ["plan", "reply", "start_from", "crop", "rotate_clockwise_degrees", "cut_out", "models", "fill",
                 "brightness", "contrast"],
    "additionalProperties": False,
}
EDIT_PROMPT = """You are fixing the website tile for one car badge ({name}). The tile shows the badge cut out of its photo,
centered on a dark background (#171a1e) with a soft shadow. Max reviewed the last proposal and asked for changes.

Images, in order: {images}.

Max's notes, oldest first (the last one is the new request):
{notes}

Choose an edit recipe that does what Max asked. Work from whichever image is the best starting point: the original
photo (to re-cut or re-crop), the live tile, or the last proposal (when it was close and only needs a tweak). The crop
box is on that start image in 0-1000 coordinates. Rotation turns the result clockwise (negative = counter-clockwise)
after cropping. Turn cut_out off to keep the start image's pixels exactly (use that when starting from a tile and
only turning, cropping, resizing or brightening it). Keep everything Max did not ask to change the way it is."""

PICK_PROMPT = """These are {n} attempts at the tile for {name}, made from Max's request: "{note}".
Score each 1-10 on how well it does what Max asked while staying a clean, upright, complete badge tile, and pick the
best (index from 0)."""
PICK_SCHEMA = {
    "type": "object",
    "properties": {"notes": {"type": "string"}, "scores": {"type": "array", "items": {"type": "integer"}},
                   "best": {"type": "integer"}},
    "required": ["notes", "scores", "best"],
    "additionalProperties": False,
}


def now():
    return datetime.now().isoformat(timespec="seconds")


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def badge_label(b):
    return " ".join(x for x in [b.get("make"), b.get("badge_name")] if x) or b["id"]


def original_for(b):
    name = os.path.basename(b.get("photo") or "")
    for d in (ORIGINALS, os.path.join(ROOT, "app", "photos")):
        p = os.path.join(d, name)
        if name and os.path.isfile(p):
            return p
    return None


def open_original(path):
    return ImageOps.exif_transpose(Image.open(path)).convert("RGB")


def rotated(rgba, deg):
    return rgba.rotate(-deg, resample=Image.BICUBIC, expand=True) if deg else rgba


def due(entry, today):
    if not entry:
        return True
    st, last = entry.get("status"), entry.get("checked")
    last = datetime.fromisoformat(last) if last else datetime.min
    if st in ("proposed", "reshoot"):
        return False
    if st == "rejected":
        return today - last > timedelta(days=RETRY_REJECTED_DAYS)
    return today - last > timedelta(days=RECHECK_DAYS)


def set_state(bid, entry=None):
    """Re-read the state file and change only this badge (the review page writes to it too)."""
    st = load_json(STATE, {})
    if entry is None:
        st.pop(bid, None)
    else:
        st[bid] = entry
    save_json(STATE, st)


def pending_redo(bid):
    meta = load_json(os.path.join(QUEUE, bid, "meta.json"), None)
    return meta if meta and meta.get("redo") else None


def untile(img):
    """Finished tile (badge on the dark #171a1e background) -> RGBA badge with a clean alpha.
    Pixels clearly different from the background mark the badge; anything enclosed by it (dark holes, black rims)
    counts as badge too, so only background reachable from the edge becomes transparent."""
    from PIL import ImageChops, ImageDraw, ImageFilter
    diff = ImageChops.difference(img, Image.new("RGB", img.size, rb.BG)).convert("L")
    mask = diff.point(lambda v: 255 if v > 22 else 0).filter(ImageFilter.MaxFilter(5))
    pad = Image.new("L", (mask.width + 4, mask.height + 4), 0)
    pad.paste(mask, (2, 2))
    ImageDraw.floodfill(pad, (0, 0), 128)
    obj = pad.crop((2, 2, pad.width - 2, pad.height - 2)).point(lambda v: 0 if v == 128 else 255)
    obj = obj.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.GaussianBlur(1.2))
    rgba = img.convert("RGBA")
    rgba.putalpha(obj)
    return rgba


def render_recipe(start, r, log):
    """Apply an EDIT_SCHEMA recipe to a PIL RGB image. Returns [(label, tile)]."""
    w, h = start.size
    c = r["crop"]
    box = [max(0, min(1000, v)) for v in (c["x0"], c["y0"], c["x1"], c["y1"])]
    if box[2] - box[0] > 20 and box[3] - box[1] > 20 and box != [0, 0, 1000, 1000]:
        start = start.crop((box[0] * w // 1000, box[1] * h // 1000, box[2] * w // 1000, box[3] * h // 1000))
    clamp = lambda v: max(0.6, min(1.6, v or 1.0))
    if abs(clamp(r["brightness"]) - 1) > 0.01:
        start = ImageEnhance.Brightness(start).enhance(clamp(r["brightness"]))
    if abs(clamp(r["contrast"]) - 1) > 0.01:
        start = ImageEnhance.Contrast(start).enhance(clamp(r["contrast"]))
    deg = max(-180, min(180, r["rotate_clockwise_degrees"]))
    fill = max(0.5, min(0.95, r["fill"] or 0.8))
    out = []
    if r["cut_out"]:
        for model in r["models"] or MODELS:
            try:
                out.append((model, rb.tile(rotated(rb.cutout(start, model), deg), 720, fill)))
            except Exception as e:
                log(f"    cutout {model} failed: {e}")
    else:
        # keep the pixels (no new cut-out); a finished tile's badge is lifted off its dark background first
        rgba = untile(start) if r["start_from"] != "original_photo" else start.convert("RGBA")
        label = "edited tile" if r["start_from"] != "original_photo" else "photo, no cut-out"
        out.append((label, rb.tile(rotated(rgba, deg), 720, fill)))
    return out


def redo(b, meta, log):
    """Max pressed "Try again" with a note: make a new proposal that follows it."""
    qdir = os.path.join(QUEUE, b["id"])
    name = badge_label(b)
    notes = meta.get("requests") or [{"note": meta["redo"]}]
    src = original_for(b)
    starts = {"live_tile": Image.open(os.path.join(qdir, "current.jpg")).convert("RGB")}
    if os.path.exists(os.path.join(qdir, "candidate.jpg")):
        starts["last_proposal"] = Image.open(os.path.join(qdir, "candidate.jpg")).convert("RGB")
    if src:
        starts["original_photo"] = open_original(src)
    names = {"original_photo": "the original photo", "live_tile": "the tile live on the site now",
             "last_proposal": "the last proposal (the one Max sent back)"}
    order = [k for k in ("original_photo", "live_tile", "last_proposal") if k in starts]
    content = [cv.image_block(starts[k]) for k in order]
    content.append({"type": "text", "text": EDIT_PROMPT.format(
        name=name, images=", ".join(f"{i + 1}) {names[k]}" for i, k in enumerate(order)),
        notes="\n".join(f"- {n['note']}" + (f" (you replied: {n['reply']})" if n.get("reply") else "")
                        for n in notes))})
    recipe = cv.ask_json(content, EDIT_SCHEMA, effort="high")
    if recipe["start_from"] not in starts:
        recipe["start_from"] = order[0]
    log(f"  {b['id']} {name}: redo \"{meta['redo']}\" -> {recipe['plan']}")
    cands = render_recipe(starts[recipe["start_from"]], recipe, log)
    if not cands:
        raise RuntimeError("no version could be rendered")
    best, score = 0, None
    if len(cands) > 1:
        pick = cv.ask_json([cv.image_block(c[1], 720) for c in cands] + [{"type": "text", "text": PICK_PROMPT.format(
            n=len(cands), name=name, note=meta["redo"])}], PICK_SCHEMA)
        best = pick["best"] if 0 <= pick["best"] < len(cands) else 0
        score = pick["scores"][best] if best < len(pick["scores"]) else None
    label, img = cands[best]
    img.save(os.path.join(qdir, "candidate.jpg"), quality=84, optimize=True, progressive=True)
    notes[-1]["reply"] = recipe["reply"]
    meta.update(method=f"{label}, from your note", reply=recipe["reply"], requests=notes, queued=now(),
                round=meta.get("round", 1) + 1)
    if score:
        meta["score_after"] = score
    for k in ("redo", "redo_at", "redo_error"):
        meta.pop(k, None)
    save_json(os.path.join(qdir, "meta.json"), meta)
    log(f"    new proposal: {label}. {recipe['reply']}")


state_reset = []  # badges whose tile was just rebuilt: forget old review results


def ensure_tiles(badges, log, dry):
    """Render tiles for badges that have none (new scans). Returns list of new tile paths."""
    os.makedirs(TILES, exist_ok=True)
    made = []
    state_reset.clear()
    for b in badges:
        out = os.path.join(TILES, f"{b['id']}.jpg")
        src = original_for(b)
        if not src:
            continue
        # a re-taken (or rotated) photo is newer than its tile: rebuild it
        if os.path.exists(out) and os.path.getmtime(out) >= os.path.getmtime(src):
            continue
        try:
            tile = rb.tile(rb.cutout(open_original(src), MODELS[0]), 720)
        except Exception as e:
            log(f"  tile for {b['id']} failed: {e}")
            continue
        if not dry:
            tile.save(out, quality=84, optimize=True, progressive=True)
        made.append(out)
        log(f"  new tile: {b['id']} {badge_label(b)}")
        state_reset.append(b["id"])
    return made


def improve(b, entry, log, dry):
    """Review one tile; maybe queue a better version. Returns the new state entry."""
    tile_path = os.path.join(TILES, f"{b['id']}.jpg")
    name = badge_label(b)
    review = cv.ask_json([cv.image_block(tile_path),
                          {"type": "text", "text": REVIEW_PROMPT.format(name=name)}], REVIEW_SCHEMA)
    entry = {**(entry or {}), "checked": now(), "score": review["score"], "problems": review["problems"],
             "seen": review["what_i_see"]}
    deg = max(-180, min(180, review["rotate_clockwise_degrees"]))
    if deg and abs(deg) < 4:
        deg = 0
    log(f"  {b['id']} {name}: score {review['score']} {review['problems']} rotate {deg}")
    if review["score"] >= 8 and not deg:
        entry["status"] = "ok"
        return entry

    src = original_for(b)
    cands = []
    if src:
        img = open_original(src)
        for model in MODELS:
            try:
                cut = rb.cutout(img, model)
            except Exception as e:
                log(f"    cutout {model} failed: {e}")
                continue
            cands.append((f"{model}, turned {deg}°" if deg else model, rb.tile(rotated(cut, deg), 720)))
    if deg:  # the cheapest fix: just turn the current tile's badge
        cur = Image.open(tile_path).convert("RGB")
        cands.append((f"current turned {deg}°", cur.rotate(-deg, resample=Image.BICUBIC, fillcolor=rb.BG)))

    best_i, scores, notes = 0, [review["score"]], ""
    if cands:
        content = [cv.image_block(tile_path, 720)] + [cv.image_block(c[1], 720) for c in cands]
        content.append({"type": "text", "text": COMPARE_PROMPT.format(n=len(content), name=name)})
        cmp = cv.ask_json(content, COMPARE_SCHEMA)
        scores, notes = cmp["scores"], cmp["notes"]
        best_i = cmp["best"] if 0 <= cmp["best"] <= len(cands) else 0
        log(f"    candidates {[c[0] for c in cands]} scores {scores} -> best {best_i}")

    cur_score = scores[0] if scores else review["score"]
    best_score = scores[best_i] if best_i < len(scores) else 0
    if best_i and best_score >= cur_score + 1:
        label, img = cands[best_i - 1]
        qdir = os.path.join(QUEUE, b["id"])
        if not dry:
            os.makedirs(qdir, exist_ok=True)
            img.save(os.path.join(qdir, "candidate.jpg"), quality=84, optimize=True, progressive=True)
            shutil.copy(tile_path, os.path.join(qdir, "current.jpg"))
            save_json(os.path.join(qdir, "meta.json"), {
                "id": b["id"], "name": name, "method": label, "score_before": cur_score,
                "score_after": best_score, "problems": review["problems"], "notes": notes, "queued": now()})
        entry.update(status="proposed", proposed=label)
        log(f"    queued for review: {label} ({cur_score} -> {best_score})")
    elif review["photo_unfixable"] or "hand_or_fingers" in review["problems"] or "blurry" in review["problems"]:
        entry.update(status="reshoot", reshoot_tip=review["reshoot_tip"])
        log(f"    needs a new photo: {review['reshoot_tip']}")
    else:
        entry["status"] = "ok" if review["score"] >= 6 else "weak"
    return entry


def telegram(text):
    cfg = load_json(os.path.expanduser("~/.config/badge-hunter/telegram.json"), {})
    if not cfg.get("token"):
        return False
    import urllib.parse, urllib.request
    data = urllib.parse.urlencode({"chat_id": cfg["chat_id"], "text": text,
                                   "disable_web_page_preview": "true"}).encode()
    with urllib.request.urlopen(f"https://api.telegram.org/bot{cfg['token']}/sendMessage", data, timeout=20) as r:
        return json.load(r).get("ok", False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--no-telegram", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="no files written, nothing published")
    a = ap.parse_args()

    def log(msg):
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)

    coll = load_json(COLLECTION, {})
    badges = coll.get("badges", coll if isinstance(coll, list) else [])
    state = load_json(STATE, {})
    log(f"image agent: {len(badges)} badges")

    new_tiles = ensure_tiles(badges, log, a.dry_run)
    if new_tiles and not a.dry_run:
        publish.commit_and_push(new_tiles, f"Image agent: tiles for {len(new_tiles)} new badge(s)", log)

    for bid in state_reset:
        state.pop(bid, None)
        if not a.dry_run:
            set_state(bid)
        shutil.rmtree(os.path.join(QUEUE, bid), ignore_errors=True)

    # Max's "Try again" notes come first and don't count against --limit
    redone = []
    for b in badges:
        meta = pending_redo(b["id"])
        if not meta or (a.only and b["id"] not in a.only) or a.dry_run:
            continue
        try:
            redo(b, meta, log)
            redone.append(badge_label(b))
        except Exception as e:
            log(f"  {b['id']} redo failed: {e}")
            traceback.print_exc()
            meta["redo_error"] = str(e)[:200]
            save_json(os.path.join(QUEUE, b["id"], "meta.json"), meta)
    if a.only and redone:
        return
    today = datetime.now()
    todo = [b for b in badges if os.path.exists(os.path.join(TILES, f"{b['id']}.jpg"))
            and not os.path.exists(os.path.join(QUEUE, b["id"], "meta.json"))
            and (b["id"] in a.only if a.only else due(state.get(b["id"]), today))]
    todo = todo[:a.limit]
    log(f"reviewing {len(todo)} tile(s)")
    queued, reshoot = [], []
    for b in todo:
        try:
            state[b["id"]] = improve(b, state.get(b["id"]), log, a.dry_run)
        except Exception as e:
            log(f"  {b['id']} failed: {e}")
            traceback.print_exc()
            continue
        if state[b["id"]]["status"] == "proposed":
            queued.append(badge_label(b))
        elif state[b["id"]]["status"] == "reshoot":
            reshoot.append(f"{badge_label(b)}: {state[b['id']].get('reshoot_tip', '')}")
        if not a.dry_run:
            set_state(b["id"], state[b["id"]])

    waiting = len([d for d in os.listdir(QUEUE)]) if os.path.isdir(QUEUE) else 0
    log(f"done: {len(queued)} queued, {len(reshoot)} need a new photo, {waiting} waiting for review")
    if (queued or reshoot or new_tiles or redone) and not a.no_telegram and not a.dry_run:
        lines = ["🖼 Badge image agent"]
        if new_tiles:
            lines.append(f"Made tiles for {len(new_tiles)} new badge(s) (live).")
        if redone:
            lines.append(f"Re-did {len(redone)} from your notes: " + ", ".join(redone[:8]))
        if queued:
            lines.append(f"{len(queued)} better version(s) ready: " + ", ".join(queued[:8]))
        if waiting:
            lines.append(f"Review {waiting}: http://100.75.67.4:8282/review")
        if reshoot:
            lines.append("Please re-photograph:\n" + "\n".join("• " + r for r in reshoot[:8]))
        try:
            telegram("\n".join(lines))
        except Exception as e:
            log(f"telegram failed: {e}")


if __name__ == "__main__":
    main()
