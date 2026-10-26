#!/usr/bin/env python3
"""
AutoCut - cut the boring parts out of gameplay videos. Free, needs only ffmpeg + Python 3.

A stretch counts as "boring" when it is BOTH quiet (game/mic audio is low) AND
visually static (little changes on screen): loading screens, menus, waiting,
AFK, dead lobbies. Loud OR moving stretches are always kept.

Command line:
    python autocut.py video.mp4                  # writes video_cut.mp4 + video_cut.txt
    python autocut.py video.mp4 --dry-run        # only writes the cut list, no video
    python autocut.py video.mp4 --nvenc          # use NVIDIA GPU encoding (much faster)

Prefer clicking? Run autocut_gui.pyw instead.

Tuning (if it cuts too much / too little):
    --min-boring 4     shortest boring stretch (seconds) worth cutting      [default 4]
    --pad 0.7          seconds of boring footage kept around each cut       [default 0.7]
    --quiet-db 14      "quiet" = this many dB below the video's normal loudness [default 14]
                       (LOWER it, e.g. 8, to cut more - useful when game music never stops)
    --motion 0.012     "static" = frame change below this (0 = identical frames) [default 0.012]
"""
import argparse
import os
import re
import statistics
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, field

__version__ = "1.0.0"

STEP = 0.5  # analysis resolution in seconds
VIDEO_EXTS = (".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".m4v", ".ts")

# Stop ffmpeg from flashing a console window for every call when run from the GUI on Windows.
_NO_WINDOW = {"creationflags": 0x08000000} if os.name == "nt" else {}


class AutoCutError(Exception):
    """A problem worth showing to the user as-is."""


class Cancelled(Exception):
    pass


@dataclass
class Settings:
    min_boring: float = 4.0
    pad: float = 0.7
    quiet_db: float = 14.0
    motion: float = 0.012
    nvenc: bool = False
    crf: int = 20


PRESETS = {
    "Balanced (recommended)": Settings(),
    "Cut more (aggressive)": Settings(min_boring=2.5, pad=0.4, quiet_db=10.0, motion=0.02),
    "Cut less (safe)": Settings(min_boring=8.0, pad=1.2, quiet_db=18.0, motion=0.008),
    "Game with constant music": Settings(quiet_db=7.0, motion=0.015),
}


@dataclass
class Result:
    source: str
    output: str
    report: str
    total: float
    removed: float
    cuts: list = field(default_factory=list)
    rendered: bool = False


class Job:
    """Holds the running ffmpeg process so another thread can cancel it."""

    def __init__(self):
        self._cancel = threading.Event()
        self._proc = None
        self._lock = threading.Lock()

    def cancel(self):
        self._cancel.set()
        with self._lock:
            if self._proc and self._proc.poll() is None:
                self._proc.kill()

    @property
    def cancelled(self):
        return self._cancel.is_set()

    def stream(self, cmd, on_line=None):
        """Run cmd, feed each stdout line to on_line, return (returncode, stderr text)."""
        if self.cancelled:
            raise Cancelled()
        with tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as err:
            with self._lock:
                self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err, stdin=subprocess.DEVNULL,
                                              text=True, errors="replace", bufsize=1, **_NO_WINDOW)
            for line in self._proc.stdout:
                if on_line:
                    on_line(line)
            rc = self._proc.wait()
            err.seek(0)
            err_text = err.read()
        if self.cancelled:
            raise Cancelled()
        return rc, err_text


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, errors="replace", **_NO_WINDOW)


def check_ffmpeg():
    """Raise AutoCutError with install help if ffmpeg/ffprobe are missing."""
    for tool in ("ffmpeg", "ffprobe"):
        try:
            run([tool, "-version"])
        except FileNotFoundError:
            hint = ("On Windows: open a terminal, run  winget install Gyan.FFmpeg  then restart this app."
                    if os.name == "nt" else "Install ffmpeg with your package manager (e.g. brew install ffmpeg).")
            raise AutoCutError(f"{tool} was not found.\n\n{hint}")


def ffmpeg_major():
    m = re.search(r"ffmpeg version n?(\d+)", run(["ffmpeg", "-version"]).stdout)
    return int(m.group(1)) if m else 0


def probe(path):
    """Return (duration, has_audio, fps, exact rate string)."""
    r = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", path])
    try:
        total = float(r.stdout.strip())
    except ValueError:
        raise AutoCutError(f"Could not read {os.path.basename(path)}. Is it a valid video file?")
    a = run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
             "-of", "csv=p=0", path])
    v = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=avg_frame_rate",
             "-of", "default=nw=1:nk=1", path])
    rate = v.stdout.strip()
    if not rate:
        raise AutoCutError(f"{os.path.basename(path)} has no video stream.")
    try:
        num, den = rate.split("/")
        fps = float(num) / float(den)
    except (ValueError, ZeroDivisionError):
        fps = 0
    if fps <= 0:
        fps, rate = 30.0, "30/1"
    return total, bool(a.stdout.strip()), fps, rate


def _time_progress(total, cb):
    """Make an on_line handler that reports progress from 'pts_time:' lines."""
    def handler(line):
        m = re.search(r"pts_time:([\d.]+)", line)
        if m and cb and total > 0:
            cb(min(float(m.group(1)) / total, 1.0))
    return handler


def analyse_audio(job, path, total, progress=None):
    """Return list of RMS dB per STEP window."""
    n = int(16000 * STEP)
    flt = (f"aresample=16000,asetnsamples=n={n}:p=0,astats=metadata=1:reset=1,"
           f"ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-")
    vals = []
    tick = _time_progress(total, progress)

    def on_line(line):
        tick(line)
        m = re.search(r"RMS_level=(-?[\d.]+|-?inf)", line)
        if m:
            v = m.group(1)
            vals.append(-120.0 if "inf" in v else max(float(v), -120.0))

    rc, err = job.stream(["ffmpeg", "-v", "error", "-nostdin", "-i", path, "-vn", "-af", flt, "-f", "null", "-"],
                         on_line)
    if rc != 0:
        raise AutoCutError(f"ffmpeg could not read the audio:\n{err.strip()[-500:]}")
    bins = int(total / STEP) + 1
    return vals[:bins] + [-120.0] * max(0, bins - len(vals))


def analyse_motion(job, path, total, progress=None):
    """Return list of frame-change scores (0..1) per STEP window (max within window)."""
    flt = "fps=4,scale=192:-2,format=gray,select='gte(scene,0)',metadata=print:key=lavfi.scene_score:file=-"
    bins = int(total / STEP) + 1
    out = [0.0] * bins
    state = {"t": None}
    tick = _time_progress(total, progress)

    def on_line(line):
        m = re.search(r"pts_time:([\d.]+)", line)
        if m:
            state["t"] = float(m.group(1))
            tick(line)
            return
        m = re.search(r"scene_score=([\d.]+)", line)
        if m and state["t"] is not None:
            i = min(int(state["t"] / STEP), bins - 1)
            out[i] = max(out[i], float(m.group(1)))

    rc, err = job.stream(["ffmpeg", "-v", "error", "-nostdin", "-i", path, "-an", "-vf", flt, "-f", "null", "-"],
                         on_line)
    if rc != 0:
        raise AutoCutError(f"ffmpeg could not read the video:\n{err.strip()[-500:]}")
    return out


def find_cuts(audio, motion, s: Settings, total):
    n = len(motion)
    if audio is not None and len(audio) >= 2:
        ref = statistics.quantiles(audio, n=4)[2]  # 75th percentile = the video's "normal loud" level
        quiet_thr = ref - s.quiet_db
        quiet = [a < quiet_thr for a in audio[:n]]
    else:
        quiet_thr = None
        quiet = [True] * n
    boring = [quiet[i] and motion[i] < s.motion for i in range(n)]

    # Fill 1-bin gaps so a single twitchy frame doesn't split a long dull stretch.
    for i in range(1, n - 1):
        if not boring[i] and boring[i - 1] and boring[i + 1]:
            boring[i] = True

    cuts, i = [], 0
    while i < n:
        if boring[i]:
            j = i
            while j < n and boring[j]:
                j += 1
            start, end = i * STEP, min(j * STEP, total)
            if end - start >= s.min_boring:
                a, b = start + s.pad, end - s.pad
                if b - a > 0.25:
                    cuts.append((a, b))
            i = j
        else:
            i += 1
    return cuts, quiet_thr


def keep_from_cuts(cuts, total):
    keep, pos = [], 0.0
    for s, e in cuts:
        if s - pos > 0.05:
            keep.append((pos, s))
        pos = e
    if total - pos > 0.05:
        keep.append((pos, total))
    return keep


def fmt(t):
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):d}:{int(m):02d}:{s:05.2f}"


def render(job, src, dst, keep, audio_present, fps, rate, s: Settings, progress=None):
    # Snap every cut to the frame grid and use half-open ranges [a, b), so the video keeps
    # exactly as much time as the audio does and the two never drift apart.
    keep = [(round(a * fps) / fps, round(b * fps) / fps) for a, b in keep]
    keep = [(a, b) for a, b in keep if b > a]
    kept = sum(b - a for a, b in keep)
    half = 0.5 / fps
    vexpr = "+".join(f"gte(t,{a - half:.6f})*lt(t,{b - half:.6f})" for a, b in keep)
    aexpr = "+".join(f"gte(t,{a:.6f})*lt(t,{b:.6f})" for a, b in keep)
    graph = f"[0:v]select='{vexpr}',setpts=N/({rate})/TB[v]"
    maps = ["-map", "[v]"]
    if audio_present:
        # Split audio into tiny 128-sample chunks first so cuts land within ~3 ms.
        graph += f";[0:a]asetnsamples=n=128:p=0,aselect='{aexpr}',asetpts=N/SR/TB[a]"
        maps += ["-map", "[a]"]
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write(graph)
        script = f.name
    venc = (["-c:v", "h264_nvenc", "-preset", "p5", "-cq", str(s.crf)] if s.nvenc
            else ["-c:v", "libx264", "-preset", "medium", "-crf", str(s.crf)])
    # ffmpeg 7+ reads a filter graph from a file with "-/filter_complex"; older versions
    # use "-filter_complex_script" (deprecated in 7.x).
    fc_opt = ["-/filter_complex", script] if ffmpeg_major() >= 7 else ["-filter_complex_script", script]
    cmd = ["ffmpeg", "-y", "-v", "error", "-nostats", "-nostdin", "-progress", "pipe:1", "-i", src,
           *fc_opt, *maps, "-r", rate, *venc, "-pix_fmt", "yuv420p"]
    if audio_present:
        cmd += ["-c:a", "aac", "-b:a", "192k"]
    cmd += ["-movflags", "+faststart", dst]

    def on_line(line):
        m = re.match(r"out_time_us=(\d+)", line)
        if m and progress and kept > 0:
            progress(min(int(m.group(1)) / 1e6 / kept, 1.0))

    try:
        rc, err = job.stream(cmd, on_line)
    except Cancelled:
        if os.path.exists(dst):
            try:
                os.remove(dst)
            except OSError:
                pass
        raise
    finally:
        os.unlink(script)
    if rc != 0:
        hint = "\n\nYour GPU may not support NVENC - untick 'Use NVIDIA GPU' and try again." if s.nvenc else ""
        raise AutoCutError(f"ffmpeg failed while rendering:\n{err.strip()[-500:]}{hint}")


def default_output(src, out_dir=None):
    base, ext = os.path.splitext(os.path.basename(src))
    folder = out_dir or os.path.dirname(os.path.abspath(src))
    return os.path.join(folder, f"{base}_cut{ext if ext.lower() in ('.mp4', '.mkv', '.mov') else '.mp4'}")


def process(src, s: Settings, output=None, dry_run=False, job=None, progress=None, status=None):
    """Analyse src, write the cut list and (unless dry_run) the cut video.

    progress(fraction 0..1) and status(text) are optional callbacks.
    """
    job = job or Job()
    say = status or (lambda _t: None)
    prog = progress or (lambda _f: None)
    if not os.path.isfile(src):
        raise AutoCutError(f"File not found: {src}")
    out = output or default_output(src)
    if os.path.abspath(out) == os.path.abspath(src):
        raise AutoCutError("Output would overwrite the original video. Pick a different output folder.")
    report = os.path.splitext(out)[0] + ".txt"

    total, audio_present, fps, rate = probe(src)
    # Progress weighting: analysis is ~45% of the work when rendering, 100% on a dry run.
    w_audio, w_motion = (0.30, 0.70) if dry_run else (0.12, 0.33)
    say("Analysing audio...")
    audio = analyse_audio(job, src, total, lambda f: prog(f * w_audio)) if audio_present else None
    say("Analysing motion...")
    motion = analyse_motion(job, src, total, lambda f: prog(w_audio + f * w_motion))
    prog(w_audio + w_motion)

    cuts, qthr = find_cuts(audio, motion, s, total)
    removed = sum(e - a for a, e in cuts)
    lines = [f"Source: {src}",
             f"Length: {fmt(total)}  ->  {fmt(total - removed)}  "
             f"(removing {fmt(removed)}, {100 * removed / total if total else 0:.0f}%)",
             f"Settings: min-boring={s.min_boring}s pad={s.pad}s quiet-db={s.quiet_db} "
             f"motion={s.motion} quiet-threshold={'n/a' if qthr is None else f'{qthr:.1f} dB'}", "",
             "CUTS (removed from the original timeline):"]
    lines += [f"  {i + 1:>3}. {fmt(a)} -> {fmt(e)}  ({e - a:.1f}s)" for i, (a, e) in enumerate(cuts)] or ["  none found"]
    with open(report, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    result = Result(src, out, report, total, removed, cuts)
    if dry_run or not cuts:
        prog(1.0)
        return result
    say("Rendering...")
    render(job, src, out, keep_from_cuts(cuts, total), audio_present, fps, rate, s,
           lambda f: prog(w_audio + w_motion + f * (1 - w_audio - w_motion)))
    prog(1.0)
    result.rendered = True
    return result


def main():
    d = Settings()
    ap = argparse.ArgumentParser(description="Cut the boring (quiet + static) parts out of gameplay videos.")
    ap.add_argument("video")
    ap.add_argument("-o", "--output")
    ap.add_argument("--dry-run", action="store_true", help="only write the cut list")
    ap.add_argument("--min-boring", type=float, default=d.min_boring)
    ap.add_argument("--pad", type=float, default=d.pad)
    ap.add_argument("--quiet-db", type=float, default=d.quiet_db)
    ap.add_argument("--motion", type=float, default=d.motion)
    ap.add_argument("--nvenc", action="store_true", help="encode on NVIDIA GPU")
    ap.add_argument("--crf", type=int, default=d.crf, help="quality, lower = better (default 20)")
    ap.add_argument("--version", action="version", version=f"AutoCut {__version__}")
    a = ap.parse_args()
    s = Settings(a.min_boring, a.pad, a.quiet_db, a.motion, a.nvenc, a.crf)

    def bar(f):
        n = int(f * 30)
        print(f"\r  [{'#' * n}{'.' * (30 - n)}] {f * 100:5.1f}%", end="", flush=True)

    def status(t):
        print(f"\n{t}", end="", flush=True)

    try:
        check_ffmpeg()
        r = process(a.video, s, a.output, a.dry_run, progress=bar, status=status)
    except (AutoCutError, Cancelled) as e:
        print()
        sys.exit(str(e) or "Cancelled.")
    except KeyboardInterrupt:
        sys.exit("\nCancelled.")
    print(f"\n\nLength: {fmt(r.total)} -> {fmt(r.total - r.removed)} "
          f"({len(r.cuts)} cut(s), {fmt(r.removed)} removed)\nCut list: {r.report}")
    if r.rendered:
        print(f"Done: {r.output}")
    elif not r.cuts:
        print("Nothing to cut with these settings. Try --min-boring 2 or --quiet-db 10.")


if __name__ == "__main__":
    main()
