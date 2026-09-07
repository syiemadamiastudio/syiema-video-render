import os, json, tempfile, subprocess
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
    return {"ok": True, "service": "syiema-video-render"}

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
            return jsonify({"error": "clip_plan is empty"}), 400

        work = Path(tempfile.mkdtemp(prefix="render_"))
        master = work / "master.mp4"
        voice = work / "voice.mp3"
        output = work / "output.mp4"

        download_file(master_url, master)
        download_file(voiceover_url, voice)

        filters, labels = [], []
        total = 0.0
        for i, clip in enumerate(clip_plan):
            start = float(clip["source_start"])
            dur = float(clip.get("clip_duration", float(clip["source_end"]) - start))
            total += dur
            label = f"v{i}"
            filters.append(f"[0:v]trim=start={start}:duration={dur},setpts=PTS-STARTPTS[{label}]")
            labels.append(f"[{label}]")

        filters.append("".join(labels) + f"concat=n={len(labels)}:v=1:a=0[vcat]")
        filters.append("[1:a]apad[aout]")

        cmd = [
            "ffmpeg","-y",
            "-i",str(master),
            "-i",str(voice),
            "-filter_complex",";".join(filters),
            "-map","[vcat]",
            "-map","[aout]",
            "-t",f"{total:.3f}",
            "-c:v","libx264",
            "-preset","veryfast",
            "-crf","20",
            "-pix_fmt","yuv420p",
            "-c:a","aac",
            "-b:a","192k",
            "-movflags","+faststart",
            str(output)
        ]
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if p.returncode != 0:
            raise RuntimeError(p.stderr[-6000:])
        return send_file(output, mimetype="video/mp4", as_attachment=True, download_name="rendered.mp4")
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT","8080")))
