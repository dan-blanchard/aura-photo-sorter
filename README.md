# aura-photo-sorter

Put photos on your Aura frames by dropping them in an iCloud shared album.

A Mac watches the album and uploads each new photo to the right frames, based on whether
it's landscape or portrait and, optionally, who's in it. Anyone you share the album with can
add photos from their own Photos app, with no Aura app or Aura account needed. When it isn't
sure who's in a photo, it asks on your phone through Home Assistant.

It works with stock frames. It talks to Aura's cloud the same way Aura's own app does, using
your Aura login. That API is unofficial and undocumented, so an Aura update could break this.

## How photos are routed

Each frame in `config.json` has an `orientation` (`landscape`, `portrait` or `any`) and
optionally a list of `people`. A new photo goes to every frame whose orientation fits and
whose people (if it has any) are in the photo. If no frame fits, it goes whole to the frames
with no people list, and Aura crops it to fit. Squares count as landscape.

Faces are only checked when some frame that takes that orientation has a people list, so
simple setups never run face matching at all. When they are checked, the sorter asks you
about the photo if:

- it's unsure about a face
- Photos' own people labels disagree with its face matcher
- it found none of the people and no frame that takes anyone fits the photo

A few setups:

**One frame.** Everything goes to it, and the sorter never asks anything.

```json
{"album": "Frame photos", "frames": [{"name": "Living room", "id": "..."}]}
```

**A family frame, plus a portrait frame per kid.** Landscape photos go to the family frame.
Portraits go to the frame of each kid in them, and a portrait with no kids goes to the family
frame, cropped. This is what I use.

```json
{"album": "Frame photos", "frames": [
  {"name": "Family", "id": "...", "orientation": "landscape"},
  {"name": "Alex's frame", "id": "...", "orientation": "portrait", "people": ["Alex"]},
  {"name": "Sam's frame", "id": "...", "orientation": "portrait", "people": ["Sam"]}
]}
```

**Two people, a frame each, plus a shared frame in the hall.** Every landscape photo goes to
the hall. Photos of Ann also go to her frame, in either orientation, and portraits of Bea go
to hers.

```json
{"album": "Frame photos", "frames": [
  {"name": "Hall", "id": "...", "orientation": "landscape"},
  {"name": "Ann's office", "id": "...", "people": ["Ann"]},
  {"name": "Bea's office", "id": "...", "orientation": "portrait", "people": ["Bea"]}
]}
```

It only adds photos. Removing a photo from the album leaves it on the frames, and videos are
skipped. Photos are uploaded at full size.

## What you need

- A Mac that stays on and logged in, signed into iCloud with Shared Albums turned on in Photos
- [uv](https://docs.astral.sh/uv/) and the Xcode command line tools (`xcode-select --install`)
- Your Aura account email and password
- For questions on your phone: Home Assistant with the companion app on your phone. Without
  it, photos that need a question wait.

## Setup

The sorter keeps everything in `~/Library/Application Support/aura-photo-sorter`, because
launchd can't read `~/Documents`.

1. **Make the shared album** in Photos and share it with whoever should be able to add photos.
   If you already have photos you want on the frames, add them after step 7: everything in
   the album at install time is marked as already handled.

2. **Log in to Aura.** This saves a login token, not your password.

   ```sh
   uv run login.py you@example.com
   ```

3. **Find your frames' IDs.**

   ```sh
   uv run --script sorter.py frames
   ```

   This lists each frame's Aura ID, serial number and name, as set in the Aura app. It leaves
   out frames other people share with you.

4. **Write `config.json`** in the sorter's folder, starting from
   [`config.example.json`](config.example.json). `name` is whatever you want to call the frame
   in the logs. If you use `people`, write each name the way it appears in Photos' People
   album (a partial match is fine: "Alex" matches "Alex Smith").

5. **Build the face gallery** (only if any frame lists `people`). This reads Photos' People
   labels and builds example faces for the people in your config, plus anyone else with enough
   photos, so the matcher can tell them apart. Name the people in Photos first. It reads
   Photos' library directly, so run it from Terminal, which needs Full Disk Access.

   ```sh
   S=~/Library/Application\ Support/aura-photo-sorter
   uv run --script build-gallery.py "$S/model" "$S/gallery.npz"
   ```

   It downloads InsightFace's `buffalo_l` model the first time, and takes a while on a big
   library.

6. **Set up Home Assistant** (optional). In `config.json`, set `ha_url` and `notify` (your
   phone's notify service, such as `mobile_app_my_phone`), and save a
   [long-lived access token](https://www.home-assistant.io/docs/authentication/#your-account-profile)
   in `ha-token` in the sorter's folder. If you also set `ha_ssh` to a login for HA's SSH
   add-on, questions include a thumbnail of the photo. To answer, press and hold the
   notification and pick who's in it.

7. **Install.**

   ```sh
   ./install.sh
   ```

   This builds a small signed helper that reads the album (macOS asks for Photos access),
   checks your config against your Aura account, marks what's already in the album as
   handled, and starts the sorter as a LaunchAgent. Rerun it after changing `sorter.py`;
   changes to `config.json` take effect when the service restarts.

To check what it would do with an image without changing anything:

```sh
uv run --script sorter.py try photo.jpg
```

## Running it

- Log: `~/Library/Logs/aura-photo-sorter/sorter.log`. It's quiet when nothing happens.
- Alive check: `last-album-read` in the sorter's folder updates every 5 minutes.
- Stop it: `launchctl bootout gui/$(id -u)/local.aura-photo-sorter`
- A photo that isn't the frame's shape is shown through a crop Aura works out by itself. If
  that crop still leaves black bars, the log says so; pick a tighter crop in the Aura app.
- If your Aura login expires, the sorter pauses, tells you, and loses nothing. Run `login.py`
  again.
- A photo added to the album twice is uploaded once, and you get one notification naming the
  copies. A new-style shared album counts against your iCloud storage, and only Photos can
  remove a photo from it.
- A photo removed from the album before it's handled is dropped quietly, and picked up again
  if it comes back.

## Privacy

Face matching runs on your Mac. The only thing that leaves it is the upload to Aura, plus a
thumbnail on your own Home Assistant when the sorter asks a question. `gallery.npz` identifies
the people in it: keep it private.

InsightFace's pretrained models, including `buffalo_l`, are licensed for non-commercial
research use only. Check
[their terms](https://github.com/deepinsight/insightface) before using this for
anything else.

## License

[0BSD](LICENSE). Almost all of this was written by [Claude Code](https://claude.com/claude-code),
working with me. The upload flow follows
[zmanowar/auraframes](https://github.com/zmanowar/auraframes)'s documentation of Aura's API.
This project is not affiliated with or endorsed by Aura.
