import os
import json
import tempfile
import subprocess
from pathlib import Path

import requests
from flask import Flask, request, jsonify, send_file

app = Flask(__name__)

TARGET_DURATION = 20.0
MAX_CLIPS = 12
MIN_CLIP_DURATION = 0.05


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
        return float(p.stdout.strip())

    except Exception:
        raise RuntimeError(
            f"Unable to detect duration: {path}"
        )


# ==========================================================
# VALIDATE FAL CLIP PLAN
# ==========================================================

def validate_clip_plan(raw_plan, master_duration):
    """
    IMPORTANT:
    FAL source_start/source_end are the edit decision.

    DO NOT:
    - extend source_end
    - move source_start
    - merge neighbouring clips
    - replace clips with continuous master footage

    Only validate/clamp impossible timestamps.
    """

    clips = []

    for clip in raw_plan:

        if (
            "source_start" not in clip
            or "source_end" not in clip
        ):
            continue

        start = float(
            clip["source_start"]
        )

        end = float(
            clip["source_end"]
        )

        # Only clamp timestamps that are outside
        # the actual master video.
        start = max(
            0.0,
            min(start, master_duration)
        )

        end = max(
            0.0,
            min(end, master_duration)
        )

        duration = end - start

        if duration < MIN_CLIP_DURATION:
            continue

        clips.append({
            "source_start": round(start, 3),
            "source_end": round(end, 3),
            "duration": round(duration, 3)
        })

    if not clips:
        raise ValueError(
            "No valid clips in clip_plan"
        )

    if len(clips) > MAX_CLIPS:
        clips = clips[:MAX_CLIPS]

    return clips


# ==========================================================
# BUILD 20 SECOND TIMELINE
# ==========================================================

def build_timeline(clips):
    """
    Preserve every FAL clip exactly.

    If FAL plan is shorter than 20 sec:
    repeat the SAME variation sequence.

    The final repeated clip may be shortened
    only to stop exactly at 20 sec.

    We NEVER extend a clip into footage that
    FAL did not select.
    """

    timeline = []

    total = 0.0
    index = 0

    while total < TARGET_DURATION - 0.001:

        source = clips[
            index % len(clips)
        ]

        start = source[
            "source_start"
        ]

        original_duration = source[
            "duration"
        ]

        remaining = (
            TARGET_DURATION - total
        )

        use_duration = min(
            original_duration,
            remaining
        )

        if use_duration >= MIN_CLIP_DURATION:

            timeline.append({
                "source_start": start,
                "source_end": round(
                    start + use_duration,
                    3
                ),
                "duration": round(
                    use_duration,
                    3
                )
            })

            total += use_duration

        index += 1

        # Safety guard
        if index > 1000:
            raise RuntimeError(
                "Unable to build 20 second timeline"
            )

    return timeline


# ==========================================================
# ATEMPO
# ==========================================================

def build_atempo(speed):
    """
    Chain atempo filters safely when required.
    """

    if speed <= 1.0001:
        return "apad"

    parts = []

    while speed > 2.0:
        parts.append(
            "atempo=2.0"
        )
        speed /= 2.0

    parts.append(
        f"atempo={speed:.6f}"
    )

    parts.append(
        "apad"
    )

    return ",".join(parts)


# ==========================================================
# HEALTH
# ==========================================================

@app.get("/")
def health():
    return {
        "ok": True,
        "service": "syiema-video-render",
        "version": "fal-exact-cut-v2"
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

        # Make sends Clip Plan as a Long String.
        if isinstance(
            clip_plan,
            str
        ):
            clip_plan = json.loads(
                clip_plan
            )

        if not isinstance(
            clip_plan,
            list
        ):
            return jsonify({
                "error":
                "clip_plan must be a JSON array"
            }), 400

        if not clip_plan:
            return jsonify({
                "error":
                "clip_plan is empty"
            }), 400


        # ==================================================
        # TEMP DIRECTORY
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
        # DOWNLOAD SOURCE FILES
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
        # FAL EXACT CUTS
        # ==================================================

        fal_plan = (
            validate_clip_plan(
                clip_plan,
                master_duration
            )
        )

        timeline = (
            build_timeline(
                fal_plan
            )
        )


        # ==================================================
        # VO SPEED
        # ==================================================

        if voice_duration > TARGET_DURATION:

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
            timeline
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
        # CONCAT VIDEO
        # ==================================================

        video_filter = (
            ";".join(filters)
            + ";"
            + "".join(video_inputs)
            + f"concat=n={len(timeline)}:"
            f"v=1:a=0,"
            f"fps=30,"
            f"setpts=PTS-STARTPTS"
            f"[vout]"
        )


        # ==================================================
        # AUDIO
        # ==================================================

        audio_filter = (
            f"[1:a]"
            f"{audio_chain},"
            f"atrim=duration={TARGET_DURATION},"
            f"asetpts=PTS-STARTPTS"
            f"[aout]"
        )


        filter_complex = (
            video_filter
            + ";"
            + audio_filter
        )


        # ==================================================
        # FFMPEG
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
            f"{TARGET_DURATION:.3f}",

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
        # VERIFY OUTPUT
        # ==================================================

        if not output.exists():
            raise RuntimeError(
                "FFmpeg did not create output file"
            )

        if output.stat().st_size < 1000:
            raise RuntimeError(
                "Rendered output is unexpectedly small"
            )


        # ==================================================
        # RETURN MP4
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


# ==========================================================
# LOCAL START
# ==========================================================

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
