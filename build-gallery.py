# /// script
# requires-python = "==3.12.*"
# dependencies = ["insightface==2.1", "numpy", "onnxruntime", "opencv-python-headless"]
# ///
"""Builds the sorter's example faces from the people named in Photos.

Usage: build-gallery.py <model dir> <out.npz> [extra.npz]
The people to recognize are those named in the sorter's config.json frames, matched to people
in Photos whose name contains theirs. Other people with enough faces are kept too, so the
matcher can tell them apart. extra.npz adds faces known some other way, with arrays emb and
person. The result identifies the people in it: keep it private.
"""
import collections
import json
import multiprocessing
import os
import random
import sqlite3
import sys
import urllib.parse

os.environ.setdefault("OMP_NUM_THREADS", "2")
import numpy as np

LIBRARY = os.path.expanduser("~/Pictures/Photos Library.photoslibrary")
HOME = os.environ.get("AURA_SORTER_HOME") or os.path.expanduser("~/Library/Application Support/aura-photo-sorter")
with open(f"{HOME}/config.json") as f:
    PEOPLE = {p.lower(): p for frame in json.load(f)["frames"] for p in frame.get("people", [])}
KID_LIMIT, OTHER_LIMIT, OTHER_MINIMUM = 900, 150, 40
MIN_SIZE, POSITION_LIMIT = 0.04, 0.05
MODEL_DIR = sys.argv[1]
app = None


def setup():
    global app
    from insightface.app import FaceAnalysis

    app = FaceAnalysis(
        name="buffalo_l", root=MODEL_DIR, allowed_modules=["detection", "recognition"],
        providers=["CPUExecutionProvider"],
    )
    app.prepare(ctx_id=-1, det_size=(960, 960), det_thresh=0.5)


def detect(job):
    import cv2

    uuid_, path = job
    image = cv2.imread(path)
    if image is None:
        return uuid_, None, []
    height, width = image.shape[:2]
    found = [
        ((float(f.bbox[0] + f.bbox[2]) / 2 / width, float(f.bbox[1] + f.bbox[3]) / 2 / height),
         f.normed_embedding.astype("float32"))
        for f in app.get(image)
    ]
    return uuid_, (width, height), found


def copy_of(u):
    for path in (f"{LIBRARY}/resources/derivatives/{u[0]}/{u}_1_105_c.jpeg",
                 f"{LIBRARY}/resources/derivatives/{u[0]}/{u}_1_102_o.jpeg",
                 f"{LIBRARY}/resources/derivatives/masters/{u[0]}/{u}_4_5005_c.jpeg"):
        if os.path.exists(path):
            return path


def main():
    # Loading once here first: a pool whose initializer fails restarts its workers forever,
    # and workers downloading the model at the same time corrupt it.
    setup()
    random.seed(7)
    db = f"{LIBRARY}/database/Photos.sqlite"
    conn = sqlite3.connect(f"file:{urllib.parse.quote(db)}?mode=ro", uri=True)
    rows = conn.execute(
        "select coalesce(nullif(p.ZDISPLAYNAME, ''), p.ZFULLNAME), a.ZUUID, f.ZCENTERX, f.ZCENTERY, f.ZNAMESOURCE"
        " from ZDETECTEDFACE f join ZPERSON p on p.Z_PK = f.ZPERSONFORFACE join ZASSET a on a.Z_PK = f.ZASSETFORFACE"
        " where a.ZTRASHEDSTATE = 0 and a.ZCOLLECTIONSHARE is null and a.ZKIND = 0 and f.ZHIDDEN = 0 and f.ZSIZE >= ?",
        (MIN_SIZE,),
    ).fetchall()
    people = collections.defaultdict(list)
    for name, u, x, y, source in rows:
        if name:
            kid = next((v for k, v in PEOPLE.items() if k in name.lower()), None)
            people[kid or name].append((u, x, y, source))
    wanted = []
    for name, faces in people.items():
        kid = name in PEOPLE.values()
        if not kid and len(faces) < OTHER_MINIMUM:
            continue
        random.shuffle(faces)
        faces.sort(key=lambda r: 0 if r[3] and r[3] > 0 else 1)  # Ones you confirmed in Photos first.
        wanted += [(name, *f[:3]) for f in faces[:KID_LIMIT if kid else OTHER_LIMIT]]
    jobs = [(u, copy_of(u)) for u in sorted({w[1] for w in wanted}) if copy_of(u)]
    print("images", len(jobs), flush=True)
    results = {}
    with multiprocessing.get_context("spawn").Pool(6, initializer=setup) as pool:
        for n, (u, size, found) in enumerate(pool.imap_unordered(detect, jobs, chunksize=4)):
            results[u] = found
            if (n + 1) % 1000 == 0:
                print(n + 1, flush=True)
    embeddings, names = [], []
    for name, u, x, y in wanted:
        best = None
        for (cx, cy), embedding in results.get(u, []):
            d = min(((cx - x) ** 2 + (cy - y) ** 2) ** 0.5, ((cx - x) ** 2 + (cy - (1 - y)) ** 2) ** 0.5)
            if best is None or d < best[0]:
                best = (d, embedding)
        if best and best[0] <= POSITION_LIMIT:
            embeddings.append(best[1])
            names.append(name)
    emb, person = np.stack(embeddings), np.array(names)
    if len(sys.argv) > 3:
        extra = np.load(sys.argv[3])
        emb, person = np.concatenate([emb, extra["emb"]]), np.concatenate([person, extra["person"]])
    print("faces", len(person), {k: int((person == k).sum()) for k in PEOPLE.values()})
    np.savez(sys.argv[2], emb=emb, person=person)


if __name__ == "__main__":
    main()
