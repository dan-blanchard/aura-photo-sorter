# /// script
# requires-python = "==3.12.*"
# dependencies = ["boto3", "insightface==2.1", "numpy", "onnxruntime", "opencv-python-headless", "pillow", "websockets"]
# ///
"""Sorts photos from a shared iCloud album onto Aura frames.

Each frame in config.json takes landscape photos, portrait photos or both, and optionally only
photos of certain people. A photo goes to every frame whose orientation fits and whose people
(if it has any) are in the photo. If none fits, it goes whole to the frames with no people
list, and Aura works out its own crop. Faces are only checked when some frame that fits the
photo's orientation has a people list. Anything unsure, and a photo with none of the people
found and no frame for anyone to take it, waits for an answer on your phone. Photos' own people labels come first when the
original is in the library, and the face matcher covers the rest. Images are uploaded at full
size, so they can be synced back later.

Runs under launchd, which can't read ~/Documents or ~/.config, so everything it needs lives in
HOME below. Log no tokens. Rules and setup: README.md.

Usage: sorter.py run                 the service
       sorter.py baseline            mark everything now in the album as already handled
       sorter.py frames              list the frames on your Aura account (read-only)
       sorter.py check               check the login and that config.json's frames are yours
       sorter.py try <image>         print what would happen to one image; changes nothing
       sorter.py classify <image>    print the faces found, as JSON (run as a child process)
"""
import base64
import calendar
import datetime
import hashlib
import io
import json
import logging
import os
import random
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid

HOME = os.environ.get("AURA_SORTER_HOME") or os.path.expanduser("~/Library/Application Support/aura-photo-sorter")
HELPER = f"{HOME}/aura-photo-album"
STATE = f"{HOME}/state.json"
WORK = f"{HOME}/work"


def read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


# config.json: the album title and a list of frames, each with its Aura id, a name, an
# orientation (landscape, portrait or any) and optionally the people whose photos go on it.
# The people must match names in the face gallery. See README.md.
CONFIG = read_json(f"{HOME}/config.json", {})
FRAMES = CONFIG.get("frames", [])
PEOPLE = sorted({p for f in FRAMES for p in f.get("people", [])})
# One character per person in a notification's answer codes.
CODES = "0123456789abcdefghijklmnopqrstuvwxyz"

TICK_SECONDS = 300
MAX_NEW_PER_TICK = 20  # A big album drop gets paced rather than sent all at once.
MAX_OPEN_QUESTIONS = 5
REASK_SECONDS = 86400
MAX_ATTEMPTS = 3
# From the 2026-11-01 dry run on the 366 photos then on the frames.
SURE, MARGIN, UNSURE = 0.35, 0.05, 0.25
MIN_FACE, MIN_DETECTION = 0.025, 0.6
SAME_IMAGE = 4.0
BAR_PIXELS = 4  # Aura's crop padding; about 1 px is rounding, real bars were over 100.  # Burst shots a second apart differ by about 6 on the same signature.
LIBRARY_MATCH = 8.0

# The Aura app's own upload bucket and Cognito pool, as in github.com/zmanowar/auraframes.
S3_BUCKET = "images.senseapp.co"
S3_POOL = "us-east-1:b92826c0-8274-43db-abff-136977c13598"
API = "https://api.pushd.com/v5"
USER_AGENT = "Aura/4.7.790 (Android 30; Client)"
# Home Assistant, for the questions on your phone. ha_url and notify (a notify service such
# as mobile_app_my_phone) in config.json, and the token in ha-token. ha_ssh (user@host for
# HA's SSH add-on) is optional: with it, questions carry a thumbnail of the photo.
HA_URL = CONFIG.get("ha_url", "").rstrip("/")
HA_SSH = CONFIG.get("ha_ssh")
HA_MEDIA = "/media/aura-sorter"
NOTIFY = f"/api/services/notify/{CONFIG.get('notify', '')}"
ACTION_PREFIX = "AURASORT"


def fits(frame, orientation):
    return frame.get("orientation", "any") in ("any", orientation)


def candidates(orientation):
    """The people who matter for a photo of this orientation: those on a frame that takes it."""
    return sorted({p for f in FRAMES if fits(f, orientation) for p in f.get("people", [])})


def route(orientation, people):
    """The names of the frames a photo goes to.

    A frame takes the photo if its orientation fits and it has no people list or one of its
    people is in the photo. If no frame does, the photo goes to every frame with no people
    list, whatever its orientation, and Aura crops it to fit. If there are none, nowhere.
    """
    chosen = [f["name"] for f in FRAMES
              if fits(f, orientation) and (not f.get("people") or set(people) & set(f["people"]))]
    return chosen or [f["name"] for f in FRAMES if not f.get("people")]


def choices(guess, orientation):
    """The answers a question offers, as (code, people). A code has one character per person.

    iOS shows up to 10 actions, so with more than three people to choose from, only each
    person alone, the guess and "None of them" are offered.
    """
    import itertools

    names = candidates(orientation)
    if len(names) <= 3:
        groups = [c for n in range(1, len(names) + 1) for c in itertools.combinations(names, n)]
    else:
        groups = [(p,) for p in names]
        if len(guess) > 1:
            groups.append(tuple(p for p in names if p in guess))
    out = [("".join(CODES[PEOPLE.index(p)] for p in g), list(g)) for g in groups]
    return out + [("NONE", [])]


log = logging.getLogger("sorter")
state_lock = threading.Lock()


def save_state(state):
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, STATE)


# --- Faces ---


def classify(path):
    """Prints the people found in one image. A child process, so the model's memory goes away."""
    import cv2
    import numpy as np
    from insightface.app import FaceAnalysis

    gallery = np.load(f"{HOME}/gallery.npz")
    embeddings, people = gallery["emb"], gallery["person"]
    names = sorted(set(people.tolist()))
    image = cv2.imread(path)
    height, width = image.shape[:2]
    # The larger size misses faces that fill the frame, so the smaller one is the fallback.
    faces = []
    for size in (960, 320):
        app = FaceAnalysis(
            name="buffalo_l", root=f"{HOME}/model", allowed_modules=["detection", "recognition"],
            providers=["CPUExecutionProvider"],
        )
        app.prepare(ctx_id=-1, det_size=(size, size), det_thresh=0.5)
        faces = app.get(image)
        if faces:
            break
    sure, unsure = {}, set()
    for face in faces:
        x1, y1, x2, y2 = (float(v) for v in face.bbox)
        if (x2 - x1) / max(width, height) < MIN_FACE or face.det_score < MIN_DETECTION:
            continue
        sims = face.normed_embedding @ embeddings.T
        scores = np.array([np.sort(sims[people == n])[-3:].mean() for n in names])
        order = np.argsort(scores)[::-1]
        best, top, gap = names[order[0]], float(scores[order[0]]), float(scores[order[0]] - scores[order[1]])
        if best not in PEOPLE:
            continue
        if top >= SURE and gap >= MARGIN:
            sure[best] = top
        elif top >= UNSURE:
            unsure.add(best)
    print(json.dumps({"sure": sorted(sure), "unsure": sorted(unsure - set(sure))}))


def run_classify(path):
    out = subprocess.run(
        [shutil.which("uv") or "uv", "run", "--script", os.path.abspath(__file__), "classify", path],
        capture_output=True, text=True, timeout=300,
    )
    if out.returncode:
        raise RuntimeError(f"classify failed: {out.stderr[-300:]}")
    return json.loads(out.stdout.strip().splitlines()[-1])


def signature(image):
    from PIL import ImageOps

    return list(ImageOps.exif_transpose(image).convert("L").resize((32, 24)).getdata())


def distance(a, b):
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def photos_labels(path, taken):
    """The people Photos names on this photo's library original. None if there's no opinion."""
    from PIL import Image

    config = read_json(f"{HOME}/config.json")
    out_dir = f"{WORK}/labels-{uuid.uuid4()}"
    os.makedirs(out_dir)
    try:
        # The helper reads Photos' database: under launchd, Python hangs on it.
        rows = json.loads(helper("labels", config["album"], str(taken), out_dir))
        with Image.open(path) as image:
            wanted = signature(image)
        best = None
        for row in rows:
            with Image.open(row["file"]) as image:
                d = distance(wanted, signature(image))
            if d <= LIBRARY_MATCH and (best is None or d < best[0]):
                best = (d, row["names"])
    except Exception as e:
        log.warning("can't read Photos' labels: %s", e)
        return None
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)
    if best is None:
        return None
    found = {p for n in best[1] for p in PEOPLE if p.lower() in n.lower()}
    return sorted(found) or None


def decide(path, taken):
    """Returns (orientation, where, names): where is "frames" with the frames' names, or "ask"
    with the best guess at who's in it. Squares count as landscape."""
    from PIL import Image

    with Image.open(path) as image:
        width, height = image.size
    orientation = "landscape" if width >= height else "portrait"
    wanted = set(candidates(orientation))
    if not wanted:
        return orientation, "frames", route(orientation, [])
    found = run_classify(path)
    sure = sorted(set(found["sure"]) & wanted)
    unsure = sorted(set(found["unsure"]) & wanted)
    labels = photos_labels(path, taken)
    labels = None if labels is None else sorted(set(labels) & wanted)
    if not sure and not unsure and not labels and any(
            fits(f, orientation) and not f.get("people") for f in FRAMES):
        # Nobody listed is in it, and a frame for anyone takes it: nothing to ask.
        return orientation, "frames", route(orientation, [])
    if unsure or not sure:
        return orientation, "ask", sorted(set(sure) | set(unsure) | set(labels or []))
    if labels and labels != sure:
        return orientation, "ask", sorted(set(sure) | set(labels))
    return orientation, "frames", route(orientation, sure)


# --- Aura ---


class LoginExpired(Exception):
    pass


def aura(method, path, login, body=None):
    request = urllib.request.Request(
        API + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "content-type": "application/json; charset=utf-8",
            "user-agent": USER_AGENT,
            "x-token-auth": login["auth_token"],
            "x-user-id": login["user_id"],
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise LoginExpired from e
        raise RuntimeError(f"{method} {path.split('?')[0]}: HTTP {e.code}") from e


def already_on_frame(path, taken, frame, login):
    """True if this frame already holds this exact shot. Copies in the album get a new id."""
    from PIL import Image

    listing = aura("GET", f"/frames/{frame}/assets.json?side_load_users=false&limit=1000", login)
    with Image.open(path) as image:
        wanted = signature(image)
        landscape = image.size[0] >= image.size[1]
    for asset in listing["assets"]:
        when = calendar.timegm(time.strptime(asset["taken_at"][:19], "%Y-%m-%dT%H:%M:%S"))
        if abs(when - taken) > 2:
            continue
        url = asset.get("landscape_url") if landscape else asset.get("portrait_url")
        with urllib.request.urlopen(url, timeout=60) as r:
            with Image.open(io.BytesIO(r.read())) as held:
                if distance(wanted, signature(held)) <= SAME_IMAGE:
                    return True
    return False


def upload(data, photo_id, taken, frame, login):
    """The app's upload: tie a new local id to the frame, put the image in S3, send its details."""
    import boto3
    from PIL import Image

    local_id = f"{photo_id}/L0/001"
    picked = aura("POST", f"/frames/{frame}/select_asset.json", login,
                  {"assets": [{"asset_local_identifier": local_id}]})
    if picked.get("number_failed"):
        raise RuntimeError("select_asset failed")
    cognito = boto3.client("cognito-identity", region_name="us-east-1")
    identity = cognito.get_id(IdentityPoolId=S3_POOL)["IdentityId"]
    creds = cognito.get_credentials_for_identity(IdentityId=identity)["Credentials"]
    s3 = boto3.client(
        "s3", aws_access_key_id=creds["AccessKeyId"], aws_secret_access_key=creds["SecretKey"],
        aws_session_token=creds["SessionToken"],
    )
    file_name = f"{uuid.uuid4()}.jpg"
    s3.put_object(Body=data, Bucket=S3_BUCKET, Key=file_name)
    with Image.open(io.BytesIO(data)) as image:
        width, height = image.size
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    taken_at = datetime.datetime.fromtimestamp(taken, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    details = aura("PUT", "/assets/batch_update.json", login, {"assets": [{
        "data_uti": "public.jpeg",
        "favorite": False,
        "file_name": file_name,
        "height": height,
        "local_identifier": local_id,
        "md5_hash": base64.b64encode(hashlib.md5(data).digest()).decode(),
        "modified_at": now,
        "orientation": 1,
        "selected": True,
        "taken_at": taken_at,
        "upload_priority": -10,
        "width": width,
    }]})
    if not details.get("successes"):
        raise RuntimeError("batch_update reported no success")
    return details["successes"][0]["id"]


# --- Home Assistant ---


def ha_creds():
    if not HA_URL:
        raise RuntimeError("no ha_url in config.json")
    with open(f"{HOME}/ha-token") as f:
        return HA_URL, f.read().strip()


def notify(payload):
    if not CONFIG.get("notify"):
        log.warning("no notify service in config.json; not sent: %s", payload.get("message"))
        return
    url, token = ha_creds()
    request = urllib.request.Request(
        url + NOTIFY, data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30):
        pass


def tell(message):
    notify({"title": "Frame photos", "message": message})


def media_copy(path):
    """A small copy on HA for the notification. It's removed once the question is answered."""
    from PIL import Image

    if not HA_SSH:
        return None
    name = f"{secrets.token_hex(12)}.jpg"
    with Image.open(path) as image:
        image.thumbnail((1024, 1024))
        out = io.BytesIO()
        image.convert("RGB").save(out, "JPEG", quality=85)
    subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", HA_SSH,
         f"mkdir -p {HA_MEDIA} && cat > {HA_MEDIA}/{name}"],
        input=out.getvalue(), check=True, capture_output=True, timeout=60,
    )
    return name


def media_remove(name):
    if not HA_SSH:
        return
    subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", HA_SSH, f"rm -f {HA_MEDIA}/{name}"],
        capture_output=True, timeout=60,
    )


def ask(photo_id, entry):
    short = photo_id[:8]
    guess = entry.get("guess") or []
    actions = [
        {"action": f"{ACTION_PREFIX}_{short}_{code}", "title": " + ".join(people) if people else "None of them"}
        for code, people in choices(guess, entry.get("orientation", "any"))
    ]
    actions.append({"action": f"{ACTION_PREFIX}_{short}_SKIP", "title": "Don't use it", "destructive": True})
    # A new tag each time: a copy that replaced an earlier one, same tag, didn't get its buttons.
    data = {"tag": f"aura-sorter-{short}-{int(time.time())}", "actions": actions}
    if entry.get("media"):
        data["image"] = f"/media/local/aura-sorter/{entry['media']}"
    notify({
        "title": "Who's in this photo?",
        "message": "Hold down to answer." + (f" Best guess: {', '.join(guess)}." if guess else ""),
        "data": data,
    })


def apply_answer(state, action):
    parts = action.split("_")
    if len(parts) != 3:
        return
    _, short, code = parts
    photo_id = next((p for p, e in state["photos"].items() if p.startswith(short) and e["status"] == "asked"), None)
    if photo_id is None:
        return
    entry = state["photos"][photo_id]
    if code == "SKIP":
        entry["status"] = "skipped"
    else:
        if code == "NONE":
            people = []
        elif all(c in CODES[:len(PEOPLE)] for c in code):
            people = [PEOPLE[CODES.index(c)] for c in code]
        else:
            return
        entry["people"] = people
        entry["frames"] = route(entry.get("orientation", "any"), people)
        entry["status"] = "send"
    log.info("%s answered: %s", short, code)
    if entry.get("media"):
        media_remove(entry.pop("media"))
    save_state(state)


def listen_for_answers():
    """Taps on the phone arrive as HA events, so no HA automation is needed for them."""
    import asyncio

    import websockets

    async def listen():
        url, token = ha_creds()
        async with websockets.connect(url.replace("http", "ws", 1) + "/api/websocket", max_size=None) as ws:
            await ws.recv()
            await ws.send(json.dumps({"type": "auth", "access_token": token}))
            reply = json.loads(await ws.recv())
            if reply.get("type") != "auth_ok":
                raise RuntimeError("HA refused the login")
            await ws.send(json.dumps({"id": 1, "type": "subscribe_events", "event_type": "mobile_app_notification_action"}))
            async for message in ws:
                event = json.loads(message)
                action = ((event.get("event") or {}).get("data") or {}).get("action", "")
                if action.startswith(ACTION_PREFIX + "_"):
                    with state_lock:
                        state = read_json(STATE)
                        apply_answer(state, action)

    while True:
        try:
            asyncio.run(listen())
        except Exception as e:  # HA restarts are normal.
            log.info("answer listener reconnecting: %s", type(e).__name__)
        time.sleep(30)


# --- The loop ---


def helper(*args):
    out = subprocess.run([HELPER, *args], capture_output=True, text=True, timeout=600)
    if out.returncode:
        raise RuntimeError(out.stderr.strip()[:200])
    return out.stdout


_screens = {}


def screen(frame_id, login):
    """The frame's screen shape from Aura, as (orientation, width, height), e.g.
    ("portrait", 3, 4) for a Walden hung tall. None if Aura doesn't say."""
    if frame_id not in _screens:
        for f in aura("GET", "/frames.json", login).get("frames", []):
            m = re.fullmatch(r"(landscape|portrait)_(\d+)_(\d+)", f.get("display_aspect_ratio") or "")
            _screens[f["id"]] = (m[1], int(m[2]), int(m[3])) if m else None
    return _screens.get(frame_id)


def check_aura_crop(asset_id, frame_id, login):
    """A photo that isn't the screen's shape is shown through Aura's crop of it, which Aura works
    out by itself shortly after an upload. Warn if that crop still leaves black bars.

    Each photo has a crop per screen shape: landscape_rect and portrait_rect (user_* when
    someone picked one in the app), with *_url_padding giving the bars left over, in pixels.
    Shapes other than 4:3 have their own, e.g. portrait_4_5. A photo with no crop at all
    still filled the screen on every frame checked, so that is only noted. Checked against
    every photo on four frames, where it flagged exactly the one that showed bars.
    """
    shape = screen(frame_id, login)
    if shape is None:
        return
    orientation, w, h = shape
    listing = aura("GET", f"/frames/{frame_id}/assets.json?side_load_users=false&limit=1000", login)
    asset = next((a for a in listing["assets"] if a["id"] == asset_id), None)
    if asset is None:
        log.warning("%s isn't on the frame's list yet, so its crop can't be checked", asset_id[:8])
        return
    if abs(asset["width"] / asset["height"] - w / h) < 0.01:
        return  # Already the screen's shape.
    name = orientation if (w, h) in ((4, 3), (3, 4)) else f"{orientation}_{w}_{h}"
    rects = [asset.get(f"user_{name}_rect"), asset.get(f"{name}_rect"), asset.get(f"auto_{name}_rect")]
    padding = asset.get(f"{name}_url_padding") or {}
    if not any(rects):
        log.info("%s has no %s crop from Aura yet", asset_id[:8], orientation)
    elif max(padding.values(), default=0) > BAR_PIXELS:
        log.warning("%s still shows with bars on %s frames; pick a tighter crop in the Aura app",
                    asset_id[:8], orientation)


def send(photo_id, entry, login):
    """Puts the photo on each of its frames. Uploaded once; later frames reuse the asset."""
    path = f"{WORK}/{photo_id}.jpg"
    frames = {f["name"]: f for f in FRAMES}
    done = entry.setdefault("frames_done", [])
    for name in entry["frames"]:
        if name in done:
            continue
        frame = frames.get(name)
        if frame is None:
            log.warning("%s: frame %s isn't in config.json any more; skipped", photo_id[:8], name)
            done.append(name)
            continue
        if already_on_frame(path, entry["taken"], frame["id"], login):
            log.info("%s is already on %s", photo_id[:8], name)
        else:
            if entry.get("asset"):
                picked = aura("POST", f"/frames/{frame['id']}/select_asset.json", login,
                              {"assets": [{"asset_id": entry["asset"]}]})
                if picked.get("number_failed"):
                    raise RuntimeError("select_asset failed")
            else:
                with open(path, "rb") as f:
                    entry["asset"] = upload(f.read(), photo_id, entry["taken"], frame["id"], login)
            # Rapid adds can race a frame's refresh and leave it short, and Aura's crop arrived
            # about 13 s after an upload in testing.
            time.sleep(30)
            check_aura_crop(entry["asset"], frame["id"], login)
            log.info("%s added to %s", photo_id[:8], name)
        done.append(name)
    if not entry["frames"]:
        log.info("%s fits no frame; skipped", photo_id[:8])
    entry["status"] = "added"


def duplicate_of(photo_id, entry, state, listed_ids, album):
    """Another copy of this exact shot still in the album, or None.

    A photo added to the album twice gets a second id with the same capture time. Burst shots
    are at least 0.1 s apart, so the time must match to 10 ms, and the image must match too
    (an edited copy keeps the time but not the pixels). Of two new copies, the first one
    checked yields to the other, so exactly one survives.
    """
    from PIL import Image

    twins = [p for p, e in state["photos"].items()
             if p != photo_id and p in listed_ids
             and e["status"] not in ("gone", "duplicate", "failed", "video_skipped")
             and abs(e["taken"] - entry["taken"]) < 0.01]
    if not twins:
        return None
    with Image.open(f"{WORK}/{photo_id}.jpg") as image:
        mine = signature(image)
    for twin in twins:
        path = f"{WORK}/{twin}.jpg"
        fetched = not os.path.exists(path)
        if fetched:
            helper("export", album, twin, path)
        try:
            with Image.open(path) as image:
                same = distance(mine, signature(image)) <= SAME_IMAGE
        finally:
            if fetched:
                os.remove(path)
        if same:
            return twin
    return None


def handle(photo_id, entry, config, login, state, listed_ids):
    path = f"{WORK}/{photo_id}.jpg"
    if entry["status"] in ("new", "send") and not os.path.exists(path):
        helper("export", config["album"], photo_id, path)
    if entry["status"] == "new":
        # Skipped before the face matcher, so a copy never asks a question or alerts.
        twin = duplicate_of(photo_id, entry, state, listed_ids, config["album"])
        if twin:
            entry.update(status="duplicate", duplicate_of=twin, told=False)
            try:  # A thumbnail makes the copy easy to find; the alert still goes without one.
                entry["media"] = media_copy(path)
            except Exception as e:
                log.warning("%s: no thumbnail for the duplicate alert: %s", photo_id[:8], e)
            os.remove(path)
            log.info("%s is a duplicate of %s in the album; skipped", photo_id[:8], twin[:8])
            return
        orientation, where, names = decide(path, entry["taken"])
        entry["orientation"] = orientation
        if where == "frames":
            entry["status"], entry["frames"] = "send", names
        else:
            entry["status"], entry["guess"] = "to_ask", names
    if entry["status"] == "send":
        send(photo_id, entry, login)
    elif entry["status"] == "to_ask":
        asked = sum(1 for e in state["photos"].values() if e["status"] == "asked")
        if asked < MAX_OPEN_QUESTIONS:
            entry["media"] = media_copy(path)
            ask(photo_id, entry)
            entry["status"], entry["asked_at"] = "asked", time.time()
            log.info("%s asked about", photo_id[:8])
    if entry["status"] in ("added", "skipped") and os.path.exists(path):
        os.remove(path)


def leave_album(photo_id, entry):
    entry["status"] = "gone"
    path = f"{WORK}/{photo_id}.jpg"
    if os.path.exists(path):
        os.remove(path)
    if entry.get("media"):
        media_remove(entry.pop("media"))
    log.info("%s left the album before it was handled; dropped", photo_id[:8])


def tell_duplicates(state, album):
    """One push per pass for the copies found in it, since a new-style shared album counts
    against iCloud storage and only Photos can remove a photo from it."""
    found = [e for e in state["photos"].values() if e["status"] == "duplicate" and not e.get("told")]
    if not found:
        return
    dates = [datetime.date.fromtimestamp(e["taken"]).strftime("%b %-d, %Y") for e in found]
    if len(found) == 1:
        message = (f"A photo taken {dates[0]} is in {album} twice. Delete either copy; "
                   "the sorter uses the other.")
    else:
        message = (f"{len(found)} photos are in {album} twice, taken {'; '.join(dates)}. "
                   "Delete one copy of each; the sorter uses the others.")
    payload = {"title": "Duplicate in the frame album", "message": message}
    image = next((e["media"] for e in found if e.get("media")), None)
    if image:
        payload["data"] = {"image": f"/media/local/aura-sorter/{image}"}
    notify(payload)
    for e in found:
        e["told"] = True


def tick(config, login):
    """One pass. Returns False if the Aura login stopped working."""
    listed = json.loads(helper("list", config["album"]))
    with open(f"{HOME}/last-album-read", "w") as f:  # Shows it's alive without filling the log.
        f.write(f"{time.time()} {len(listed)}\n")
    active = ("new", "send", "to_ask")
    with state_lock:
        state = read_json(STATE)
        for row in listed:
            entry = state["photos"].get(row["id"])
            if entry is None:
                status = "video_skipped" if row["video"] else "new"
                state["photos"][row["id"]] = {"status": status, "taken": row["taken"], "attempts": 0}
            elif entry["status"] == "gone":
                entry.update(status="new", attempts=0)
                log.info("%s is back in the album", row["id"][:8])
        # A photo that leaves the album before it's handled is dropped quietly, not retried
        # into a "couldn't be added" alert. Copies of photos already in the album have been
        # seen to appear in a batch and vanish before export (removed, or iCloud dropping
        # duplicates). If one comes back it is queued again.
        listed_ids = {row["id"] for row in listed}
        for photo_id, entry in state["photos"].items():
            if entry["status"] in active + ("asked",) and photo_id not in listed_ids:
                leave_album(photo_id, entry)
        for photo_id, entry in state["photos"].items():
            if entry["status"] != "duplicate":
                continue
            if photo_id not in listed_ids:
                # The copy was deleted: its thumbnail on HA has done its job.
                if entry.get("media"):
                    media_remove(entry.pop("media"))
                    log.info("%s (duplicate) removed from the album", photo_id[:8])
            elif state["photos"].get(entry["duplicate_of"], {}).get("status") == "gone":
                # The copy in use was deleted before it reached a frame; use this one instead.
                if entry.get("media"):
                    media_remove(entry.pop("media"))
                entry.update(status="new", attempts=0)
                entry.pop("duplicate_of"), entry.pop("told", None)
                log.info("%s: the copy in use left the album; handling this one", photo_id[:8])
        save_state(state)
    work = [p for p, e in state["photos"].items() if e["status"] in active][:MAX_NEW_PER_TICK]
    for photo_id in work:
        with state_lock:
            state = read_json(STATE)
            entry = state["photos"][photo_id]
            try:
                handle(photo_id, entry, config, login, state, listed_ids)
            except LoginExpired:
                save_state(state)
                return False
            except Exception as e:
                if "no such photo in the album" in str(e):  # Removed since this tick's listing.
                    leave_album(photo_id, entry)
                    save_state(state)
                    continue
                entry["attempts"] += 1
                log.warning("%s failed (%d): %s", photo_id[:8], entry["attempts"], e)
                if entry["attempts"] >= MAX_ATTEMPTS:
                    entry["status"] = "failed"
                    tell("A photo from the album couldn't be added. The sorter's log has the details.")
            save_state(state)
    with state_lock:
        state = read_json(STATE)
        try:
            tell_duplicates(state, config["album"])
        except Exception as e:  # Not marked told, so the next pass tries again.
            log.warning("duplicate alert failed: %s", e)
        for photo_id, entry in state["photos"].items():
            if entry["status"] == "asked" and time.time() - entry["asked_at"] > REASK_SECONDS:
                ask(photo_id, entry)
                entry["asked_at"] = time.time()
        save_state(state)
    return True


def run():
    os.makedirs(WORK, exist_ok=True)
    config = read_json(f"{HOME}/config.json")
    login = read_json(f"{HOME}/aura-login.json")
    if read_json(STATE) is None:
        raise SystemExit("no state; run baseline first")
    if CONFIG.get("notify") and HA_URL:
        threading.Thread(target=listen_for_answers, daemon=True).start()
    else:
        log.warning("Home Assistant isn't set up, so photos that need a question will wait")
    log.info("started, %d photos known", len(read_json(STATE)["photos"]))
    told_login = False
    while True:
        try:
            if tick(config, login):
                told_login = False
            elif not told_login:
                told_login = True
                log.warning("the Aura login stopped working")
                tell("The frame photo sorter's Aura login stopped working, so it's paused. Nothing is lost.")
        except Exception as e:
            log.warning("tick failed: %s", e)
        time.sleep(TICK_SECONDS)


def baseline():
    config = read_json(f"{HOME}/config.json")
    if read_json(STATE) is not None:
        raise SystemExit("state already exists; not overwriting it")
    listed = json.loads(helper("list", config["album"]))
    save_state({"photos": {r["id"]: {"status": "baseline", "taken": r["taken"], "attempts": 0} for r in listed}})
    print(f"marked {len(listed)} photos as already handled")


def my_frames(login):
    """Frames on this account that are real hardware: owned by this login, with a serial.
    The listing also has frames shared with you and, sometimes, a placeholder from Aura's
    gift flow."""
    listing = aura("GET", "/frames.json", login)
    return [f for f in listing.get("frames", [])
            if f.get("user_id") == login["user_id"] and f.get("hw_serial")]


def frames():
    login = read_json(f"{HOME}/aura-login.json")
    if login is None:
        raise SystemExit("no aura-login.json; run login.py first")
    for f in my_frames(login):
        print(f"{f['id']}  {f.get('hw_serial')}  {f.get('name')}")


def check():
    """Read-only: the login works, config.json makes sense, and its frames are yours."""
    login = read_json(f"{HOME}/aura-login.json")
    if login is None:
        raise SystemExit("no aura-login.json; run login.py first")
    if not CONFIG.get("album") or not FRAMES:
        raise SystemExit('config.json needs "album" and at least one frame')
    try:
        mine = {f["id"]: f for f in my_frames(login)}
    except LoginExpired:
        raise SystemExit("Aura refused the login; run login.py again")
    bad = False
    names = [f.get("name") for f in FRAMES]
    for frame in FRAMES:
        name, frame_id = frame.get("name"), frame.get("id")
        problems = []
        if not name or names.count(name) > 1:
            problems.append("needs a name of its own")
        if frame.get("orientation", "any") not in ("landscape", "portrait", "any"):
            problems.append("orientation must be landscape, portrait or any")
        if frame_id not in mine:
            problems.append(f"{frame_id} is not one of your frames")
        if problems:
            print(f"WRONG {name}: {'; '.join(problems)}")
            bad = True
        else:
            who = ", ".join(frame.get("people", [])) or "anyone"
            print(f"ok    {name} = {mine[frame_id].get('name')} ({mine[frame_id].get('hw_serial')}):"
                  f" {frame.get('orientation', 'any')}, {who}")
    for orientation in ("landscape", "portrait"):
        if not any(fits(f, orientation) for f in FRAMES):
            print(f"note  no frame takes {orientation} photos, so they go to the frames with no people list")
    if PEOPLE:
        if len(PEOPLE) > len(CODES):
            print(f"WRONG more than {len(CODES)} people")
            bad = True
        if os.path.exists(f"{HOME}/gallery.npz"):
            import numpy as np

            known = set(np.load(f"{HOME}/gallery.npz")["person"].tolist())
            for person in PEOPLE:
                if person not in known:
                    print(f"WRONG {person} has no faces in gallery.npz")
                    bad = True
        else:
            print("WRONG frames list people, but there's no gallery.npz; run build-gallery.py")
            bad = True
        if not CONFIG.get("notify"):
            print("note  no notify service, so photos that need a question will wait")
    if bad:
        raise SystemExit(1)


def try_one(path):
    taken = float(sys.argv[3]) if len(sys.argv) > 3 else 0
    print(decide(path, taken))


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "classify":
        classify(sys.argv[2])
    elif command == "try":
        try_one(sys.argv[2])
    elif command == "baseline":
        baseline()
    elif command == "frames":
        frames()
    elif command == "check":
        check()
    elif command == "run":
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        random.seed()
        run()
    else:
        raise SystemExit(__doc__)
