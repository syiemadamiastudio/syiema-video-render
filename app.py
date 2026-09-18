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
            "FFPROBE ERROR:\n" + p.stderr[-3000:]
        )

    try:
        return float(p.stdout.strip())

    except Exception:
        raise RuntimeError(
            f"Unable to detect duration: {path}"
        )


# ==========================================================
# VALIDATE FAL PLAN
# ==========================================================

def validate_clip_plan(raw_plan, master_duration):
    """
    FAL controls the creative edit.

    We preserve:
    - source_start
    - source_end
    - clip order

    We DO NOT:
    - extend clips
    - repeat clips
    - add footage
    - reorder clips
    - merge clips
    """

    clips = []

    for clip in raw_plan:

        if (
            "source_start" not in clip
            or "source_end" not in clip
        ):
            continue

        try:
            start = float(clip["source_start"])
            end = float(clip["source_end"])
        except (TypeError, ValueError):
            continue

        # Clamp only impossible timestamps
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
# ATEMPO
# ==========================================================

def build_atempo(speed):
    """
    FFmpeg atempo supports 0.5–2.0 safely.
    Chain filters when outside that range.
    """

    parts = []

    while speed > 2.0:
        parts.append("atempo=2.0")
        speed /= 2.0

    while speed < 0.5:
        parts.append("atempo=0.5")
        speed /= 0.5

    parts.append(
        f"atempo={speed:.6f}"
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
        "version": "fal-diversity-v3"
    }


# ==========================================================
# RENDER
# ==========================================================

@app.post("/render")
def render():

    try:

        data = request.get_json(force=True)

        master_url = data["master_url"]
        voiceover_url = data["voiceover_url"]
        clip_plan = data["clip_plan"]

        # Make sends Clip Plan as Long String
        if isinstance(clip_plan, str):
            clip_plan = json.loads(clip_plan)

        if not isinstance(clip_plan, list):
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
        # TEMP
        # ==================================================

        work = Path(
            tempfile.mkdtemp(prefix="render_")
        )

        master = work / "master.mp4"
        voice = work / "voice.mp3"
        output = work / "rendered.mp4"


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
        # PROBE
        # ==================================================

        master_duration = probe_duration(master)
        voice_duration = probe_duration(voice)


        # ==================================================
        # PRESERVE FAL EDIT
        # ==================================================

        clips = validate_clip_plan(
            clip_plan,
            master_duration
        )

        raw_video_duration = sum(
            clip["duration"]
            for clip in clips
        )

        if raw_video_duration <= 0:
            raise ValueError(
                "Invalid total clip duration"
            )


        # ==================================================
        # VIDEO NORMALIZATION
        # ==========================================================
        #
        # Example:
        #
        # FAL total = 18 sec
        # Need final = 20 sec
        #
        # setpts multiplier:
        # 20 / 18 = 1.111
        #
        # FAL total = 22 sec
        # 20 / 22 = 0.909
        #
        # This changes playback speed slightly,
        # NOT the creative clip selection.
        # ==================================================

        video_pts_multiplier = (
            TARGET_DURATION
            / raw_video_duration
        )


        # ==================================================
        # VIDEO CLIPS
        # ==================================================

        filters = []
        video_inputs = []

        for i, clip in enumerate(clips):

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


        # ==================================================
        # CONCAT + NORMALIZE TO 20 SEC
        # ==================================================

        video_filter = (
            ";".join(filters)
            + ";"
            + "".join(video_inputs)
            + f"concat=n={len(clips)}:v=1:a=0,"
            + f"setpts={video_pts_multiplier:.8f}*PTS,"
            + "fps=30,"
            + "setpts=PTS-STARTPTS"
            + "[vout]"
        )


        # ==================================================
        # VOICEOVER
        # ==========================================================
        #
        # If VO >20 sec:
        # speed it up to fit.
        #
        # If VO <20 sec:
        # keep natural speed and pad silence.
        # ==================================================

        if voice_duration > TARGET_DURATION:

            voice_speed = (
                voice_duration
                / TARGET_DURATION
            )

            audio_chain = (
                build_atempo(voice_speed)
                + ",apad"
            )

        else:

            audio_chain = "apad"


        audio_filter = (
            f"[1:a]"
            f"{audio_chain},"
            f"atrim=duration={TARGET_DURATION},"
            f"asetpts=PTS-STARTPTS"
            f"[aout]"
        )


        # ==================================================
        # FILTER COMPLEX
        # ==================================================

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
        # VERIFY
        # ==================================================

        if not output.exists():
            raise RuntimeError(
                "FFmpeg did not create output"
            )

        if output.stat().st_size < 1000:
            raise RuntimeError(
                "Rendered output is unexpectedly small"
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


# ==========================================================
# START
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
