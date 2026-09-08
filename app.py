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
            for chunk in r.iter_content(1024 * 1024):
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

        # Make may send clip_plan as JSON text
        if isinstance(clip_plan, str):
            clip_plan = json.loads(clip_plan)

        if not clip_plan:
            return jsonify({
                "error": "clip_plan is empty"
            }), 400

        # ==========================================================
        # TEMP WORK DIRECTORY
        # ==========================================================

        work = Path(
            tempfile.mkdtemp(prefix="render_")
        )

        master = work / "master.mp4"
        voice = work / "voice.mp3"
        output = work / "output.mp4"

        # ==========================================================
        # DOWNLOAD MASTER + VOICEOVER
        # ==========================================================

        download_file(master_url, master)
        download_file(voiceover_url, voice)

        # ==========================================================
        # LOW-MEMORY VIDEO RENDER
        #
        # Instead of loading every trim into one filter_complex,
        # process ONE segment at a time.
        # This keeps RAM usage much lower.
        # ==========================================================

        segment_paths = []
        total = 0.0

        for i, clip in enumerate(clip_plan):

            start = float(
                clip["source_start"]
            )

            if "clip_duration" in clip:
                dur = float(
                    clip["clip_duration"]
                )

            elif "duration" in clip:
                dur = float(
                    clip["duration"]
                )

            else:
                dur = (
                    float(clip["source_end"])
                    - start
                )

            if dur <= 0:
                raise ValueError(
                    f"Invalid duration for clip {i}"
                )

            total += dur

            segment = (
                work
                / f"segment_{i:02d}.mp4"
            )

            segment_paths.append(
                segment
            )

            # ------------------------------------------------------
            # Render one small clip at a time
            # ------------------------------------------------------

            seg_cmd = [
                "ffmpeg",
                "-y",

                # Seek before decoding
                "-ss",
                f"{start:.3f}",

                "-i",
                str(master),

                "-t",
                f"{dur:.3f}",

                # Remove master audio
                "-an",

                # Low-memory / fast encoding
                "-c:v",
                "libx264",

                "-preset",
                "ultrafast",

                "-crf",
                "23",

                "-pix_fmt",
                "yuv420p",

                # Important for low memory
                "-threads",
                "1",

                "-movflags",
                "+faststart",

                str(segment)
            ]

            p = subprocess.run(
                seg_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )

            if p.returncode != 0:
                raise RuntimeError(
                    "SEGMENT RENDER ERROR:\n"
                    + p.stderr[-6000:]
                )

        # ==========================================================
        # CONCAT SEGMENTS
        # ==========================================================

        concat_list = (
            work / "concat.txt"
        )

        with open(
            concat_list,
            "w",
            encoding="utf-8"
        ) as f:

            for segment in segment_paths:
                f.write(
                    f"file '{segment}'\n"
                )

        joined = (
            work / "joined.mp4"
        )

        concat_cmd = [
            "ffmpeg",
            "-y",

            "-f",
            "concat",

            "-safe",
            "0",

            "-i",
            str(concat_list),

            # No re-encode here
            "-c",
            "copy",

            "-movflags",
            "+faststart",

            str(joined)
        ]

        p = subprocess.run(
            concat_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        if p.returncode != 0:
            raise RuntimeError(
                "CONCAT ERROR:\n"
                + p.stderr[-6000:]
            )

        # ==========================================================
        # ADD VOICEOVER
        #
        # Original JSON2Video workflow used speed = 1.12,
        # so reproduce it here using atempo=1.12.
        # ==========================================================

        final_cmd = [
            "ffmpeg",
            "-y",

            "-i",
            str(joined),

            "-i",
            str(voice),

            "-filter_complex",
            "[1:a]atempo=1.12,apad[aout]",

            "-map",
            "0:v:0",

            "-map",
            "[aout]",

            # Keep rendered video as-is
            "-c:v",
            "copy",

            "-c:a",
            "aac",

            "-b:a",
            "192k",

            # Stop at final video duration
            "-t",
            f"{total:.3f}",

            "-movflags",
            "+faststart",

            str(output)
        ]

        p = subprocess.run(
            final_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        if p.returncode != 0:
            raise RuntimeError(
                "FINAL AUDIO MIX ERROR:\n"
                + p.stderr[-6000:]
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
