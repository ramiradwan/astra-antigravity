"""Render an honestly labeled playback of the measured live CLI reviewer trace.

Requires Pillow and ffmpeg. No model calls, browser capture or fabricated product
UI. The measured trace and verification summary remain alongside the silent MP4
and WebVTT captions; raw private diagnostics are never read by this renderer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import textwrap


def timestamp(seconds):
    milliseconds = round(seconds * 1000)
    return f"{milliseconds//3600000:02}:{milliseconds//60000%60:02}:{milliseconds//1000%60:02}.{milliseconds%1000:03}"


def render(public, output):
    from PIL import Image, ImageDraw, ImageFont
    summary = json.loads((public / "review-summary.json").read_text(encoding="utf-8"))
    rows = [json.loads(line) for line in (public / "review-trace.jsonl").read_text(encoding="utf-8").splitlines()]
    if (not summary.get("live_execution") or len(summary.get("tasks", [])) != 4
            or not all(task.get("parent_verification_passed") for task in summary["tasks"])
            or not rows or rows[-1]["kind"] != "complete"):
        raise ValueError("A complete live verification trace is required; failure cannot be rendered as success")
    if any(row["at_seconds"] < rows[index-1]["at_seconds"] for index, row in enumerate(rows) if index):
        raise ValueError("Recorded timestamps must be monotonic")
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise ValueError("ffmpeg and ffprobe are required")
    output.mkdir(parents=True, exist_ok=False)
    frames = output / "frames"
    frames.mkdir()
    font_path = next((path for path in (
        Path("C:/Windows/Fonts/consola.ttf"), Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"))
                     if path.is_file()), None)
    font = lambda size: ImageFont.truetype(str(font_path), size) if font_path else ImageFont.load_default(size=size)
    title_font, body_font, small_font = font(32), font(23), font(17)
    labels = {"intro": "Live reviewer run", "installed": "Bundled release startup", "readiness": "Read-only preflight",
              "availability": "Unavailable-model notice", "negative": "Capability / schema boundary",
              "receipt": "Background delegation", "parent_work": "Independent parent work",
              "terminal": "Compact terminal delivery", "verified": "Independent output verification",
              "complete": "Run complete", "resume": "Resume saved evidence"}
    manifest_lines = []
    captions = ["WEBVTT", ""]
    video_time = 0.0
    for index, row in enumerate(rows):
        recorded = row["at_seconds"]
        gap = rows[index+1]["at_seconds"]-recorded if index+1 < len(rows) else 7.0
        duration = min(7.0, max(3.0, gap))
        image = Image.new("RGB", (1280, 720), "#0E1018")
        draw = ImageDraw.Draw(image)
        draw.text((45, 28), "Delegate to Antigravity | CLI reviewer walkthrough", font=title_font, fill="#FFFFFF")
        draw.text((45, 76), "Rendered live CLI playback | Quiet intervals shortened | Restart gaps omitted", font=small_font, fill="#C2C7D7")
        draw.rounded_rectangle((40, 124, 1240, 622), radius=14, fill="#191D2B", outline="#343C56", width=2)
        draw.text((65, 145), labels.get(row["kind"], row["kind"]), font=title_font, fill="#C5AFF5")
        draw.text((910, 158), f"Recorded T+{recorded:.1f}s", font=small_font, fill="#C2C7D7")
        lines = textwrap.wrap(row["message"], 82) + [""]
        for key, value in row["data"].items():
            if key == "archive_sha256":
                lines.extend(["Bundled archive SHA-256:", value])
            else:
                display = json.dumps(value, ensure_ascii=True) if not isinstance(value, str) else value
                lines.extend(textwrap.wrap(f"{key}: {display}", 83))
        if len(lines) > 14:
            raise ValueError("Trace scene exceeds readable bounds; do not truncate review evidence")
        for line_index, line in enumerate(lines):
            draw.text((65, 209 + line_index*27), line, font=body_font, fill="#EEF0F8")
        draw.rectangle((45, 644, 1235, 649), fill="#343C56")
        draw.rectangle((45, 644, 45 + round(1190*(index+1)/len(rows)), 649), fill="#A78BDD")
        draw.text((45, 666), f"Step {index+1}/{len(rows)} | Owner CLI accounts | Synthetic fixture data", font=small_font, fill="#C2C7D7")
        frame = frames / f"{index:04}.png"
        image.save(frame)
        manifest_lines.extend([f"file 'frames/{frame.name}'", f"duration {duration:.3f}"])
        captions.extend([str(index+1), f"{timestamp(video_time)} --> {timestamp(video_time+duration)}", row["message"], ""])
        video_time += duration
    manifest_lines.append(f"file 'frames/{frames.joinpath(f'{len(rows)-1:04}.png').name}'")
    playlist = output / "frames.txt"
    playlist.write_text("\n".join(manifest_lines)+"\n", encoding="utf-8")
    video = output / "astra-antigravity-0.9.1-cli-review.mp4"
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "1",
                    "-i", str(playlist), "-vf", "fps=20,format=yuv420p", "-c:v", "libx264",
                    "-crf", "18", "-movflags", "+faststart", "-t", f"{video_time:.3f}", str(video)], check=True, timeout=120)
    measured = json.loads(subprocess.check_output([ffprobe, "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,duration", "-of", "json", str(video)], timeout=15))["streams"][0]
    actual_duration = float(measured["duration"])
    if measured["width"] != 1280 or measured["height"] != 720 or abs(actual_duration-video_time) > 0.1:
        raise ValueError("Encoded video dimensions/duration disagree with the trace playback")
    (output / "astra-antigravity-0.9.1-cli-review.vtt").write_text("\n".join(captions), encoding="utf-8")
    proof = {"video": video.name, "sha256": hashlib.sha256(video.read_bytes()).hexdigest(),
             "width": 1280, "height": 720, "rendered_scenes": len(rows), "duration_seconds": actual_duration,
             "original_elapsed_seconds": rows[-1]["at_seconds"],
             "representation": "rendered playback of actual CLI trace; quiet intervals shortened; restart gaps omitted",
             "chatgpt_ui_recording": False, "official_submission_ready": False}
    (output / "video-evidence.json").write_text(json.dumps(proof, indent=2)+"\n", encoding="utf-8")
    print(json.dumps(proof))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public", type=Path, required=True, help="Public-safe trace folder from record_review.py")
    parser.add_argument("--output", type=Path, required=True, help="New video output folder")
    args = parser.parse_args()
    render(args.public.resolve(), args.output.resolve())
