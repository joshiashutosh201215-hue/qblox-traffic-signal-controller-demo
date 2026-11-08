"""Narration: Piper neural TTS (offline). Caches one WAV per sentence by content hash.

    ttsvenv/bin/python tts.py VOICE.onnx OUTDIR < sentences.json   ->  writes OUTDIR/<hash>.wav, prints durations JSON
"""
import hashlib, json, os, re, sys, wave

# spoken forms for acronyms and identifiers the voice would otherwise mangle
SAY = [
    (r"\bR(\d+)-0?(\d+)\b", lambda m: f"rule R {m.group(1)} dash {m.group(2)}"),
    (r"\bI(\d{1,2})\b", lambda m: f"I {m.group(1)}"),
    (r"\bD(\d)\b", lambda m: f"D {m.group(1)}"),
    (r"\bGNSS\b", "G N S S"), (r"\bPTP\b", "P T P"), (r"\bMQTT\b", "M Q T T"), (r"\bmTLS\b", "mutual T L S"),
    (r"\bTLS\b", "T L S"), (r"\bTCXO\b", "T C X O"), (r"\bAES-CMAC\b", "A E S C-MAC"), (r"\bCMAC\b", "C-MAC"),
    (r"\bEd25519\b", "Ed 2 5 5 1 9"), (r"\bSHA-256\b", "SHA 256"), (r"\bppm\b", "p p m"), (r"\bCANopen\b", "CAN open"),
    (r"\bIEEE 1588\b", "I triple E 15 88"), (r"\bMMU\b", "M M U"), (r"\bRTOS\b", "R toss"), (r"\bHAL\b", "hal"),
    (r"\bGUI\b", "gooey"), (r"\bAPI\b", "A P I"), (r"\bREST\b", "rest"), (r"\bHIL\b", "H I L"), (r"\bLWT\b", "last will"),
    (r"\bQoS ?1\b", "quality of service one"), (r"\bUDP\b", "U D P"), (r"\bNMEA\b", "N M E A"), (r"\bSQLite\b", "S Q Lite"),
    (r"\bJSON\b", "jason"), (r"\bC\+\+17\b", "C plus plus 17"), (r"\bC\+\+", "C plus plus"), (r"\bEMCY\b", "emergency"),
    (r"\bNMT\b", "N M T"), (r"\bTPDO\b", "T P D O"), (r"\bCiA 301\b", "C I A 301"), (r"\bACLs?\b", "A C L"),
    (r"\bFastAPI\b", "Fast A P I"), (r"\bpaho\b", "pah-hoe"), (r"\blibmosquitto\b", "lib mosquitto"), (r"\bOpenSSL\b", "Open S S L"),
    (r"\bA/B\b", "A B"), (r"\bIDM\b", "I D M"), (r"\bCI\b", "C I"), (r"\bms\b", "milliseconds"), (r"\bUTC\b", "U T C"),
    (r"\bpcap\b", "pee-cap"), (r"\bRFC\b", "R F C"), (r"\bPI\b", "P I"), (r"\bBMCA\b", "B M C A"), (r"\bTAI\b", "T A I"),
    (r"\bLED\b", "L E D"), (r"\bWAN\b", "wan"), (r"\bmain\.cpp\b", "main dot C P P"), (r"\.cpp\b", " dot C P P"),
    (r"\.hpp\b", " dot H P P"), (r"\.py\b", " dot pie"), (r"\.c\b", " dot C"), (r"\.js\b", " dot J S"), (r"\bctest\b", "C test"),
    (r"\bCMake\b", "C make"), (r"\bWebSocket\b", "web socket"), (r"\bIEC 62443\b", "I E C 6 2 4 4 3"), (r"\bvcan\b", "V CAN"),
    (r"\bTSCF\b", "T S C F"), (r"\bTSCA\b", "T S C A"), (r"\bOK\b", "okay"), (r"\bstd::", "standard "), (r"\bnoexcept\b", "no-except"),
    (r"\bRTT\b", "round trip"), (r"\bSAN\b", "S A N"), (r"\bCA\b", "C A"), (r"\bP-256\b", "P 256"), (r"_", " "),
]


def spoken(text):
    for pat, rep in SAY:
        text = re.sub(pat, rep, text)
    return text.replace("—", ",").replace("→", "to").replace("×", "times").replace("≤", "at most").replace("±", "plus or minus")


def key(text):
    return hashlib.sha1(spoken(text).encode()).hexdigest()[:16]


if __name__ == "__main__":
    from piper import PiperVoice, SynthesisConfig
    voice_path, out = sys.argv[1], sys.argv[2]
    os.makedirs(out, exist_ok=True)
    voice = PiperVoice.load(voice_path)
    cfg = SynthesisConfig(length_scale=1.02, noise_scale=0.6, noise_w_scale=0.75)
    res = {}
    for text in json.load(sys.stdin):
        fn = os.path.join(out, key(text) + ".wav")
        if not os.path.exists(fn):
            with wave.open(fn, "wb") as w:
                voice.synthesize_wav(spoken(text), w, syn_config=cfg)
        with wave.open(fn) as w:
            res[text] = round(w.getnframes() / w.getframerate(), 3)
    json.dump(res, sys.stdout)
