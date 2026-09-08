import os
import json
import tempfile
import subprocess
from pathlib import Path

import requests
from flask import Flask, request, jsonify, send_file

app = Flask(__name__)

TARGET_DURATION = 20.0
MAX_CLIPS = 7


# ==========================================================
# DOWNLOAD
# ==========================================================

def download_file(url, dst):
    headers = {
        "User-Agent": "Mozilla/5.0"
    }

    with requests.get(
        url,
        stream=True,
        timeout=120,
        allow_redirects=True,
        headers=headers
    ) as r:

        r.raise_for_status()

        content_type = (
            r.headers.get("Content-Type", "")
            .lower()
        )

        # Google Drive login/error page instead of actual media
        if "text/html" in content_type:
            raise RuntimeError(
                f"Download returned HTML instead of media: {url}"
            )

        with open(dst, "wb") as f:
            for chunk in r.iter_content(
                chunk_size=1024 * 1024
            ):
                if chunk:
                    f.write(chunk)

    if os.path.getsize(dst) < 1000:
        raise RuntimeError(
            f"Downloaded file is unexpectedly small: {dst}"
        )


# ==========================================================
# FFPROBE
# ==========================================================

def probe_duration(path):
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path)
    ]

    p = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    if p.returncode != 0:
        raise RuntimeError(
            "FFPROBE ERROR:\n"
            + p.stderr[-3000:]
        )

    try:
        return float(
            p.stdout.strip()
        )

    except Exception:
        raise RuntimeError(
            f"Unable to detect duration: {path}"
        )


# ==========================================================
# CLIP PLAN
# ==========================================================

def normalize_clip_plan(
    raw_plan,
    master_duration
):
    """
    Ignore FAL's written duration.
    Timestamp arithmetic is source of truth.

    Keep max 7 clips.
    Extend existing real clips when total <20s.
    Shorten clips when total >20s.
    """

    clips = []

    for clip in raw_plan:

        start = float(
            clip["source_start"]
        )

        end = float(
            clip["source_end"]
        )

        start = max(
            0.0,
            min(start, master_duration)
        )

        end = max(
            0.0,
            min(end, master_duration)
        )

        duration = end - start

        if duration <= 0:
            continue

        clips.append({
            "source_start": start,
            "source_end": end,
            "duration": duration
        })

    if not clips:
        raise ValueError(
            "No valid clips in clip_plan"
        )

    # Avoid giant FAL clip plans
    if len(clips) > MAX_CLIPS:
        clips = clips[:MAX_CLIPS]

    total = sum(
        c["duration"]
        for c in clips
    )

    # ======================================================
    # SHORT PLAN -> extend existing clips
    # ======================================================

    remaining = (
        TARGET_DURATION - total
    )

    if remaining > 0.001:

        # Spread additional time across clips
        while remaining > 0.001:

            changed = False

            for c in clips:

                available = (
                    master_duration
                    - c["source_end"]
                )

                if available <= 0:
                    continue

                add = min(
                    available,
                    remaining,
                    1.0
                )

                c["source_end"] += add
                c["duration"] += add

                remaining -= add
                changed = True

                if remaining <= 0.001:
                    break

            if not changed:
                break

        # If forward extension not enough,
        # extend starts backwards
        if remaining > 0.001:

            for c in reversed(clips):

                available = (
                    c["source_start"]
                )

                if available <= 0:
                    continue

                add = min(
                    available,
                    remaining
                )

                c["source_start"] -= add
                c["duration"] += add

                remaining -= add

                if remaining <= 0.001:
                    break

    if remaining > 0.01:
        raise ValueError(
            "Not enough real MASTER footage "
            "to build 20 seconds"
        )

    # ======================================================
    # LONG PLAN -> shorten
    # ======================================================

    total = sum(
        c["duration"]
        for c in clips
    )

    excess = (
        total - TARGET_DURATION
    )

    if excess > 0.001:

        for c in reversed(clips):

            # Keep minimum useful clip length
            reducible = max(
                0.0,
                c["duration"] - 0.5
            )

            cut = min(
                reducible,
                excess
            )

            c["source_end"] -= cut
            c["duration"] -= cut

            excess -= cut

            if excess <= 0.001:
                break

    # ======================================================
    # ROUND
    # ======================================================

    cleaned = []

    for c in clips:

        start = round(
            c["source_start"], 3
        )

        duration = round(
            c["duration"], 3
        )

        end = round(
            start + duration, 3
        )

        cleaned.append({
            "source_start": start,
            "source_end": end,
            "duration": duration
        })

    # Final tiny rounding correction
    total = round(
        sum(
            c["duration"]
            for c in cleaned
        ),
        3
    )

    diff = round(
        TARGET_DURATION - total,
        3
    )

    if abs(diff) > 0:

        c = cleaned[-1]

        new_duration = round(
            c["duration"] + diff,
            3
        )

        new_end = round(
            c["source_start"]
            + new_duration,
            3
        )

        if (
            new_duration > 0
            and new_end <= master_duration
        ):
            c["duration"] = new_duration
            c["source_end"] = new_end

    return cleaned


# ==========================================================
# ATEMPO
# ==========================================================

def build_atempo(speed):
    """
    FFmpeg atempo is safest between 0.5 and 2.0.
    Chain filters if speed is larger.
    """

    if speed <= 1.0001:
        return "apad"

    parts = []

    while speed > 2.0:
        parts.append("atempo=2.0")
        speed /= 2.0

    parts.append(
        f"atempo={speed:.6f}"
    )

    parts.append("apad")

    return ",".join(parts)


# ==========================================================
# HEALTH
# ==========================================================

@app.get("/")
def health():
    return {
        "ok": True,
        "service": "syiema-video-render-fast"
    }


# ==========================================================
# RENDER
# ==========================================================

@app.post("/render")
def render():

    try:

        data = request.get_json(
            force=True
        )

        master_url = data[
            "master_url"
        ]

        voiceover_url = data[
            "voiceover_url"
        ]

        clip_plan = data[
            "clip_plan"
        ]

        if isinstance(
            clip_plan,
            str
        ):
            clip_plan = json.loads(
                clip_plan
            )

        if not clip_plan:
            return jsonify({
                "error":
                "clip_plan is empty"
            }), 400

        # ==================================================
        # TEMP
        # ==================================================

        work = Path(
            tempfile.mkdtemp(
                prefix="render_"
            )
        )

        master = (
            work / "master.mp4"
        )

        voice = (
            work / "voice.mp3"
        )

        output = (
            work / "rendered.mp4"
        )

        # ==================================================
        # DOWNLOAD
        # ==================================================

        download_file(
            master_url,
            master
        )

        download_file(
            voiceover_url,
            voice
        )

        # ==================================================
        # DURATIONS
        # ==================================================

        master_duration = (
            probe_duration(master)
        )

        voice_duration = (
            probe_duration(voice)
        )

        # ==================================================
        # FIX FAL PLAN
        # ==================================================

        fixed_plan = (
            normalize_clip_plan(
                clip_plan,
                master_duration
            )
        )

        # ==================================================
        # VO SPEED
        # ==================================================

        if (
            voice_duration
            > TARGET_DURATION
        ):

            voice_speed = (
                voice_duration
                / TARGET_DURATION
            )

        else:

            voice_speed = 1.0

        audio_chain = (
            build_atempo(
                voice_speed
            )
        )

        # ==================================================
        # VIDEO FILTERS
        # ==================================================

        filters = []
        video_inputs = []

        for i, clip in enumerate(
            fixed_plan
        ):

            start = clip[
                "source_start"
            ]

            duration = clip[
                "duration"
            ]

            label = f"v{i}"

            filters.append(
                f"[0:v]"
                f"trim=start={start}:"
                f"duration={duration},"
                f"setpts=PTS-STARTPTS"
                f"[{label}]"
            )

            video_inputs.append(
                f"[{label}]"
            )

        # ==================================================
        # CONCAT + 30 FPS
        # ==================================================

        video_filter = (
            ";".join(filters)
            + ";"
            + "".join(video_inputs)
            + f"concat=n={len(fixed_plan)}:"
            f"v=1:a=0,"
            f"fps=30"
            f"[vout]"
        )

        audio_filter = (
            f"[1:a]"
            f"{audio_chain}"
            f"[aout]"
        )

        filter_complex = (
            video_filter
            + ";"
            + audio_filter
        )

        # ==================================================
        # FAST ONE-PASS RENDER
        # ==================================================

        cmd = [
            "ffmpeg",
            "-y",

            "-i",
            str(master),

            "-i",
            str(voice),

            "-filter_complex",
            filter_complex,

            "-map",
            "[vout]",

            "-map",
            "[aout]",

            "-c:v",
            "libx264",

            "-preset",
            "ultrafast",

            "-crf",
            "28",

            "-pix_fmt",
            "yuv420p",

            "-threads",
            "0",

            "-c:a",
            "aac",

            "-b:a",
            "128k",

            "-t",
            "20.000",

            "-movflags",
            "+faststart",

            str(output)
        ]

        p = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True
        )

        if p.returncode != 0:

            raise RuntimeError(
                "FFMPEG RENDER ERROR:\n"
                + p.stderr[-8000:]
            )

        # ==================================================
        # RETURN
        # ==================================================

        return send_file(
            output,
            mimetype="video/mp4",
            as_attachment=True,
            download_name="rendered.mp4"
        )

    except Exception as e:

        return jsonify({
            "error": str(e)
        }), 500


if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=int(
            os.environ.get(
                "PORT",
                "8080"
            )
        )
    )
