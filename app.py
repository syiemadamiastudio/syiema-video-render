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
MIN_CLIPS = 4


def download_file(url, dst):
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(dst, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)


def probe_duration(path):
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
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
            "FFPROBE ERROR:\n" + p.stderr[-3000:]
        )

    try:
        return float(p.stdout.strip())
    except Exception:
        raise RuntimeError(
            f"Could not detect duration for {path}"
        )


def normalize_clip_plan(raw_plan, master_duration):
    """
    Ignore FAL's written duration field.
    Real duration always comes from:
        source_end - source_start

    Then force final plan to exactly 20.0 seconds.
    """

    clips = []

    for i, clip in enumerate(raw_plan):
        start = float(clip["source_start"])
        end = float(clip["source_end"])

        start = max(0.0, min(start, master_duration))
        end = max(0.0, min(end, master_duration))

        real_duration = end - start

        if real_duration <= 0:
            continue

        clips.append({
            "index": i,
            "source_start": start,
            "source_end": end,
            "duration": real_duration
        })

    if len(clips) < MIN_CLIPS:
        raise ValueError(
            f"clip_plan has only {len(clips)} valid clips; minimum is {MIN_CLIPS}"
        )

    # If FAL outputs 8, 10, 13 clips etc:
    # preserve first clip as hook, then keep strongest/longest clips.
    if len(clips) > MAX_CLIPS:
        first = clips[0]

        remaining = sorted(
            clips[1:],
            key=lambda x: x["duration"],
            reverse=True
        )[:MAX_CLIPS - 1]

        selected = [first] + remaining

        # Restore original creative order
        clips = sorted(
            selected,
            key=lambda x: x["index"]
        )

    actual_total = sum(
        c["duration"] for c in clips
    )

    if actual_total <= 0:
        raise ValueError("Invalid clip_plan duration")

    # Scale existing real clips proportionally toward 20 seconds.
    scale = TARGET_DURATION / actual_total

    for c in clips:
        desired = c["duration"] * scale

        # Cannot extend beyond end of MASTER
        max_forward = master_duration - c["source_start"]

        c["duration"] = min(
            desired,
            max_forward
        )

        c["source_end"] = (
            c["source_start"] + c["duration"]
        )

    # If some clips hit MASTER boundary, distribute remaining time
    # into other existing clips.
    total = sum(c["duration"] for c in clips)
    remaining = TARGET_DURATION - total

    if remaining > 0.0001:
        for c in clips:
            available = (
                master_duration - c["source_end"]
            )

            if available <= 0:
                continue

            add = min(
                available,
                remaining
            )

            c["duration"] += add
            c["source_end"] += add
            remaining -= add

            if remaining <= 0.0001:
                break

    # If still short, extend clip starts backwards using real footage.
    if remaining > 0.0001:
        for c in reversed(clips):
            available = c["source_start"]

            if available <= 0:
                continue

            add = min(
                available,
                remaining
            )

            c["source_start"] -= add
            c["duration"] += add
            remaining -= add

            if remaining <= 0.0001:
                break

    if remaining > 0.01:
        raise ValueError(
            f"Cannot expand real MASTER footage to {TARGET_DURATION}s"
        )

    # If rounding/scaling caused total slightly above 20,
    # trim excess from longest clips.
    total = sum(c["duration"] for c in clips)
    excess = total - TARGET_DURATION

    if excess > 0.0001:
        for c in sorted(
            clips,
            key=lambda x: x["duration"],
            reverse=True
        ):
            reducible = max(
                0.0,
                c["duration"] - 0.2
            )

            cut = min(
                reducible,
                excess
            )

            c["duration"] -= cut
            c["source_end"] -= cut
            excess -= cut

            if excess <= 0.0001:
                break

    # Round cleanly
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

    # Final exact correction after rounding
    final_total = sum(
        c["duration"] for c in cleaned
    )

    diff = round(
        TARGET_DURATION - final_total,
        3
    )

    if abs(diff) > 0:
        # Try adding/removing difference from last usable clip
        for c in reversed(cleaned):

            new_duration = round(
                c["duration"] + diff,
                3
            )

            new_end = round(
                c["source_start"] + new_duration,
                3
            )

            if (
                new_duration > 0
                and new_end <= master_duration
            ):
                c["duration"] = new_duration
                c["source_end"] = new_end
                break

    # Absolute final validation
    verified_total = round(
        sum(
            c["source_end"] - c["source_start"]
            for c in cleaned
        ),
        3
    )

    if verified_total != TARGET_DURATION:
        raise ValueError(
            f"Normalized clip total is {verified_total}s, expected 20.0s"
        )

    return cleaned


@app.get("/")
def health():
    return {
        "ok": True,
        "service": "syiema-video-render"
    }


@app.post("/render")
def render():
    try:
        data = request.get_json(force=True)

        master_url = data["master_url"]
        voiceover_url = data["voiceover_url"]
        clip_plan = data["clip_plan"]

        if isinstance(clip_plan, str):
            clip_plan = json.loads(clip_plan)

        if not clip_plan:
            return jsonify({
                "error": "clip_plan is empty"
            }), 400

        work = Path(
            tempfile.mkdtemp(prefix="render_")
        )

        master = work / "master.mp4"
        voice = work / "voice_input"
        output = work / "rendered.mp4"

        # Download source files
        download_file(
            master_url,
            master
        )

        download_file(
            voiceover_url,
            voice
        )

        # Detect real durations
        master_duration = probe_duration(
            master
        )

        voice_duration = probe_duration(
            voice
        )

        # ======================================================
        # AUTO-CORRECT FAL CLIP PLAN
        # ======================================================

        fixed_plan = normalize_clip_plan(
            clip_plan,
            master_duration
        )

        # ======================================================
        # AUTO-FIT VOICEOVER
        # ======================================================

        # If VO >20s, speed it up enough to finish inside 20s.
        # Example:
        # 23s -> 1.15x
        # 27s -> 1.35x
        #
        # If VO <=20s, keep natural 1.0x and pad silence.
        if voice_duration > TARGET_DURATION:
            voice_speed = (
                voice_duration
                / TARGET_DURATION
            )
        else:
            voice_speed = 1.0

        # ======================================================
        # BUILD VIDEO FILTER
        # ======================================================

        filters = []
        video_inputs = []

        for i, clip in enumerate(fixed_plan):

            start = clip["source_start"]
            duration = clip["duration"]

            label = f"v{i}"

            filters.append(
                f"[0:v]"
                f"trim=start={start}:duration={duration},"
                f"setpts=PTS-STARTPTS"
                f"[{label}]"
            )

            video_inputs.append(
                f"[{label}]"
            )

        video_filter = (
            ";".join(filters)
            + ";"
            + "".join(video_inputs)
            + f"concat=n={len(fixed_plan)}:v=1:a=0[vout]"
        )

        # Audio: speed if needed, then pad so final always reaches 20s
        if voice_speed > 1.0001:
            audio_filter = (
                f"[1:a]"
                f"atempo={voice_speed:.6f},"
                f"apad"
                f"[aout]"
            )
        else:
            audio_filter = (
                "[1:a]apad[aout]"
            )

        filter_complex = (
            video_filter
            + ";"
            + audio_filter
        )

        # ======================================================
        # FINAL ONE-PASS RENDER
        # ======================================================

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
            "26",

            "-pix_fmt",
            "yuv420p",

            "-threads",
            "0",

            "-c:a",
            "aac",

            "-b:a",
            "160k",

            # FINAL OUTPUT ALWAYS 20.0s
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
