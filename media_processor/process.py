"""Runs ONLY inside the offline resource-limited container; no credentials."""
import hashlib
import base64
import json
import math
import subprocess
import sys
import time
from pathlib import Path

MAX_BYTES = 20 * 1024 * 1024


def run(args, timeout=45):
    result = subprocess.run(args, check=True, capture_output=True, timeout=timeout)
    if len(result.stdout) > 256 * 1024:
        raise ValueError("oversized tool response")
    return result.stdout


def signatures_current():
    files = list(Path("/var/lib/clamav").glob("daily.c[vl]d"))
    if not files or time.time() - max(p.stat().st_mtime for p in files) > 48 * 3600:
        raise ValueError("malware signatures are stale")
    run(["clamscan", "--version"])
    run(["ffmpeg", "-version"])


def inspect_video(path, *, rendition=False):
    if not 0 < path.stat().st_size <= MAX_BYTES:
        raise ValueError("invalid size")
    with path.open("rb") as source:
        magic = source.read(16)
    if magic[4:8] != b"ftyp" and magic[:4] != b"\x1aE\xdf\xa3":
        raise ValueError("invalid container magic")
    metadata = json.loads(run([
        "ffprobe", "-v", "error", "-protocol_whitelist", "file", "-show_entries",
        "format=format_name,duration:stream=codec_type,codec_name,width,height,duration,nb_frames,avg_frame_rate",
        "-of", "json", str(path),
    ]))
    container = set(metadata["format"]["format_name"].split(","))
    if not container.intersection({"mov", "mp4", "webm"}):
        raise ValueError("unsupported container")
    reported_duration = metadata["format"].get("duration")
    duration = float(reported_duration) if reported_duration is not None else None
    streams = metadata.get("streams", [])
    video = [s for s in streams if s["codec_type"] == "video"]
    if len(video) != 1 or len(streams) > 2 or any(s["codec_type"] not in {"video", "audio"} for s in streams):
        raise ValueError("unsupported streams")
    # Browser-recorded WebM can omit duration. It still must decode fully and
    # pass the rendition duration check; no -t trim ever hides a longer source.
    if (duration is None and rendition) or (duration is not None and (not math.isfinite(duration) or not 0 < duration <= 30)):
        raise ValueError("invalid duration")
    for stream in streams:
        if stream.get("duration") is not None:
            stream_duration = float(stream["duration"])
            if not math.isfinite(stream_duration) or not 0 < stream_duration <= 30:
                raise ValueError("invalid stream duration")
        if stream["codec_name"] not in {"h264", "hevc", "vp8", "vp9", "av1", "aac", "opus", "vorbis", "mp3"}:
            raise ValueError("unsupported codec")
    width, height = int(video[0]["width"]), int(video[0]["height"])
    if not 16 <= width <= 3840 or not 16 <= height <= 3840 or width * height > 3840 * 2160:
        raise ValueError("invalid dimensions")
    if rendition and (video[0]["codec_name"] != "h264" or len(streams) != 1 or max(width, height) > 1920):
        raise ValueError("invalid rendition")
    return {"duration": duration, "width": width, "height": height}


def process(source, target):
    signatures_current()
    run(["clamscan", "--no-summary", "--max-filesize=20M", "--max-scansize=40M", "--alert-exceeds-max=yes", str(source)])
    inspect_video(source)
    # Fully decode; never silently trim a >30s source. Strip audio, tags, subtitles, attachments.
    run([
        "ffmpeg", "-v", "error", "-xerror", "-nostdin", "-protocol_whitelist", "file", "-i", str(source),
        "-map", "0:v:0", "-an", "-sn", "-dn", "-map_metadata", "-1", "-map_chapters", "-1",
        "-vf", "scale='min(1920,iw)':'min(1920,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2", "-r", "30",
        "-c:v", "libx264", "-preset", "fast", "-crf", "22", "-maxrate", "4800k", "-bufsize", "4800k", "-pix_fmt", "yuv420p", "-threads", "1",
        "-movflags", "+faststart", "-y", str(target),
    ], timeout=90)
    result = inspect_video(target, rendition=True)
    run(["clamscan", "--no-summary", "--max-filesize=20M", "--max-scansize=40M", "--alert-exceeds-max=yes", str(target)])
    result["sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
    result["rendition_base64"] = base64.b64encode(target.read_bytes()).decode("ascii")
    return result


if __name__ == "__main__":
    try:
        if sys.argv[1:] == ["--health"]:
            signatures_current()
            print("ready")
        else:
            print(json.dumps(process(Path("/input/source"), Path("/output/demo.mp4"))))
    except Exception:
        # Never echo hostile parser output or file contents into host logs.
        print("Video failed validation.", file=sys.stderr)
        sys.exit(1)
