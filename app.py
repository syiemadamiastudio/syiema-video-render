import os
import json
import tempfile
import subprocess
from pathlib import Path

import requests
from flask import Flask, request, jsonify, send_file

app = Flask(__name__)


def download_file(url, dst):
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()

        with open(dst, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)


@app.get("/")
def health():
    return {
        "ok": True,
        "service": "syiema-video-render"
    }


@app.post("/render")
def render():
    try:
        # ==========================================================
        # INPUT
        # ==========================================================

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

        # ==========================================================
        # TEMP FILES
        # ==========================================================

        work = Path(tempfile.mkdtemp(prefix="render_"))

        master = work / "master.mp4"
        voice = work / "voice.mp3"
        output = work / "rendered.mp4"

        # ==========================================================
        # DOWNLOAD INPUT FILES
        # ==========================================================

        download_file(master_url, master)
        download_file(voiceover_url, voice)

        # ==========================================================
        # BUILD ONE FILTER GRAPH
        # ==========================================================

        filters = []
        concat_inputs = []
        total_duration = 0.0

        for i, clip in enumerate(clip_plan):

            start = float(clip["source_start"])

            if "clip_duration" in clip:
                duration = float(clip["clip_duration"])

            elif "duration" in clip:
                duration = float(clip["duration"])

            else:
                duration = (
                    float(clip["source_end"])
                    - start
                )

            if duration <= 0:
                raise ValueError(
                    f"Invalid duration for clip {i}"
                )

            total_duration += duration

            label = f"v{i}"

            filters.append(
                f"[0:v]"
                f"trim=start={start}:duration={duration},"
                f"setpts=PTS-STARTPTS"
                f"[{label}]"
            )

            concat_inputs.append(
                f"[{label}]"
            )

        filter_complex = (
            ";".join(filters)
            + ";"
            + "".join(concat_inputs)
            + f"concat=n={len(clip_plan)}:v=1:a=0[vout];"
            + "[1:a]atempo=1.12,apad[aout]"
        )

        # ==========================================================
        # ONE-PASS FINAL RENDER
        # ==========================================================

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

            "-t",
            f"{total_duration:.3f}",

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

        # ==========================================================
        # RETURN MP4
        # ==========================================================

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
