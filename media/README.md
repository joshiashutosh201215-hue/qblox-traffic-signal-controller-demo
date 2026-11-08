# Demo videos

| File | Length | Content |
|---|---|---|
| `01_Demo_Walkthrough_5min.mp4` | ~5:25 | Black-box tour recorded from the live system: city, cabinet, decisions and traceability, detector fault, upstream failure → degraded → fixed time, time sync (GNSS → PTP → holdover, spoof rejected), center link lost, safety unit trip and technician reset, forged/replayed messages, rule edit → validate → replay → two-person release → staged deploy → activation at a cycle boundary, telemetry and logs |
| `02_Code_Walkthrough_10min.mp4` | ~11:00 | Where each part is implemented and why: repository = architecture, technology choices, controller entry point and cyclic executive, HAL, CANopen, peer-message checks, current-cycle lookup, time servo, phase controller, rule interpreter, signed A/B configuration and hash-chained records, MQTT/mTLS, safety unit, rule language and validator, release and deployment, testbench, tests and findings, real vs emulated |
| `transcripts/*.srt` | | Narration script with timings. Captions are already burned into the picture; the transcripts are kept in a separate folder under different names so video players do not overlay them a second time |

Both are 1920×1080 H.264 + AAC. In the Git repository they are attached to the GitHub release v1.0
(https://github.com/joshiashutosh201215-hue/qblox-traffic-signal-controller-demo/releases/tag/v1.0), not committed. The narration is a synthetic voice
(Piper, offline neural TTS, voice `en_US-ryan-high`); the files in `transcripts/` are the full script if you
prefer to record your own voice over the video.

Everything in video 1 happened live: each fault was injected through the bench, and each reaction
came from the real controllers, safety units and control center. Waits for slow effects (for example
a neighbour going silent, which takes up to one cycle) are cut, and an amber "⏩ N s later" badge marks
every cut.

## Rebuilding (after changing the demo)

```
# once: Piper TTS in a venv + a voice (about 120 MB)
python3 -m venv --without-pip ttsvenv && python3 -m pip --python ttsvenv/bin/python install piper-tts
curl -L -o voices/en_US-ryan-high.onnx      https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/ryan/high/en_US-ryan-high.onnx
curl -L -o voices/en_US-ryan-high.onnx.json https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/ryan/high/en_US-ryan-high.onnx.json
export TTS_DIR=<folder holding ttsvenv/ and voices/>   # default: media/build/tts

# video 1: start from a clean, warmed-up system, keep the machine otherwise idle while recording
./demo.sh reset && ./demo.sh start        # wait ~2 min until all 20 are ACTUATED
python3 media/tools/v1_demo.py            # narration (cached) + live recording, ~9 min
python3 media/tools/assemble.py media/build/v1 media/01_Demo_Walkthrough_5min.mp4

# video 2: slides are rendered from the current source (excerpts are found by text anchors)
python3 media/tools/v2_code.py
python3 media/tools/assemble.py media/build/v2 media/02_Code_Walkthrough_10min.mp4 --slides
```

| Tool | Role |
|---|---|
| `tools/v1_demo.py` | Video 1 script: narration sentences with timed GUI actions and fault injections |
| `tools/director.py`, `tools/cdp.py`, `tools/overlay.js` | Headless Chrome via the DevTools protocol: screencast frames, chapter/subtitle overlay, highlights, cuts |
| `tools/v2_code.py`, `tools/slides.py` | Video 2 script and slide renderer (Pygments code excerpts, highlighted per sentence) |
| `tools/tts.py` | Offline narration, one cached WAV per sentence |
| `tools/assemble.py` | Frames + narration → MP4 (ffmpeg, loudness-normalised) + SRT |

Keep the host otherwise idle while recording video 1: a multi-second CPU stall stops every
controller's watchdog toggle, and the safety units then (correctly) put all intersections into flash.
