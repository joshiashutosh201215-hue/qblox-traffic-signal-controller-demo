"""Frames + narration timeline -> H.264/AAC MP4 (+ .srt subtitles).

    python3 assemble.py BUILD_DIR OUT.mp4
BUILD_DIR/timeline.json comes from director.py (live recording) or slides.py (code walkthrough):
  chunks  [[t_start, t_end], ...]      recorded wall-clock intervals, played back to back
  frames  [[t, "fNNNNNN.jpg"], ...]    frame shown from t until the next frame (within its chunk)
  speech  [{t, wav, text, dur}, ...]   narration placed at wall-clock t
"""
import array, json, os, subprocess, sys, wave

RATE = 22050


def srt_time(s):
    ms = int(round(s * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def main(bdir, out):
    bdir, out = os.path.abspath(bdir), os.path.abspath(out)
    tl = json.load(open(os.path.join(bdir, "timeline.json")))
    chunks, frames = tl["chunks"], tl["frames"]
    fdir = os.path.join(bdir, "frames")
    # video: map wall time -> video time chunk by chunk
    lines, offs, vt = [], [], 0.0
    last_file = frames[0][1]
    for cs, ce in chunks:
        offs.append((cs, ce, vt))
        fr = [(t, f) for t, f in frames if cs <= t < ce]
        if not fr or fr[0][0] > cs + 0.02:
            prev = [f for t, f in frames if t < cs]
            fr.insert(0, (cs, prev[-1] if prev else (fr[0][1] if fr else last_file)))
        for i, (t, f) in enumerate(fr):
            end = fr[i + 1][0] if i + 1 < len(fr) else ce
            d = max(end - t, 0.001)
            lines.append(f"file '{os.path.join(fdir, f)}'\nduration {d:.4f}")
            last_file = f
        vt += ce - cs
    lines.append(f"file '{os.path.join(fdir, last_file)}'")
    open(os.path.join(bdir, "concat.txt"), "w").write("\n".join(lines) + "\n")
    total = vt

    def v_of(t):
        for cs, ce, o in offs:
            if cs - 0.05 <= t <= ce:
                return o + max(0.0, t - cs)
        raise ValueError(f"speech at {t} outside recorded chunks")

    # audio: place each sentence
    pcm = array.array("h", bytes(2 * int((total + 1) * RATE)))
    srt = []
    for i, s in enumerate(tl["speech"]):
        at = v_of(s["t"])
        with wave.open(s["wav"]) as w:
            assert w.getframerate() == RATE and w.getsampwidth() == 2 and w.getnchannels() == 1
            a = array.array("h", w.readframes(w.getnframes()))
        p = int(at * RATE)
        need = p + len(a) - len(pcm)
        if need > 0:
            pcm.extend(bytes(2 * need))
        pcm[p:p + len(a)] = a
        srt.append(f"{i + 1}\n{srt_time(at)} --> {srt_time(at + s['dur'])}\n{s['text']}\n")
    apath = os.path.join(bdir, "narration.wav")
    with wave.open(apath, "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(RATE)
        w.writeframes(pcm.tobytes())
    # transcript kept away from the MP4 under another name: players auto-load a same-name .srt and would
    # draw it over the captions already burned into the picture
    tdir = os.path.join(os.path.dirname(out), "transcripts")
    os.makedirs(tdir, exist_ok=True)
    open(os.path.join(tdir, os.path.basename(os.path.splitext(out)[0]) + ".transcript.srt"), "w").write("\n".join(srt))
    fo = max(total - 1.2, 0)
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", os.path.join(bdir, "concat.txt"),
           "-i", apath, "-vf", f"fps=30,format=yuv420p,fade=t=in:st=0:d=0.8,fade=t=out:st={fo:.2f}:d=1.2",
           "-af", f"loudnorm=I=-16:TP=-1.5:LRA=11,afade=t=out:st={fo:.2f}:d=1.2", "-ar", "48000",
           "-c:v", "libx264", "-preset", "medium", "-crf", "24", "-tune", "stillimage" if "--slides" in sys.argv else "animation",
           "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", "-t", f"{total:.2f}", out]
    subprocess.run(cmd, check=True)
    print(f"{out}: {total:.1f} s, {len(tl['speech'])} sentences")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
