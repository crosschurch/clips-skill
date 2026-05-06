#!/usr/bin/env python3
"""
Transcribe audio/video using faster-whisper (int8 CPU mode) and write
a Whisper-compatible JSON to <output_dir>/<stem>.json.

Usage: python3 transcribe_faster.py <audio_file> <output_dir> [output_stem]
  output_stem defaults to the audio file's stem (without extension).
"""

import sys
import os
import json

from faster_whisper import WhisperModel

audio_file = sys.argv[1]
output_dir = sys.argv[2]
stem = sys.argv[3] if len(sys.argv) > 3 else os.path.splitext(os.path.basename(audio_file))[0]
out_path = os.path.join(output_dir, stem + ".json")

print(f"  Loading faster-whisper medium (int8 CPU)...")
model = WhisperModel("medium", device="cpu", compute_type="int8")

print(f"  Transcribing: {os.path.basename(audio_file)}")
segments_iter, info = model.transcribe(
    audio_file,
    language="en",
    word_timestamps=True,
    beam_size=5,
)

print(f"  Duration: {info.duration:.0f}s ({info.duration/60:.1f} min)")

segments = []
for seg in segments_iter:
    words = []
    if seg.words:
        for w in seg.words:
            words.append({"word": w.word, "start": w.start, "end": w.end, "probability": w.probability})
    segments.append({
        "id": seg.id,
        "seek": seg.seek,
        "start": seg.start,
        "end": seg.end,
        "text": seg.text,
        "tokens": seg.tokens,
        "temperature": seg.avg_logprob,
        "avg_logprob": seg.avg_logprob,
        "compression_ratio": seg.compression_ratio,
        "no_speech_prob": seg.no_speech_prob,
        "words": words,
    })
    if seg.id % 30 == 0:
        print(f"  {seg.start:.0f}s / {info.duration:.0f}s ({100*seg.start/info.duration:.0f}%)")

result = {
    "text": " ".join(s["text"].strip() for s in segments),
    "segments": segments,
    "language": "en",
}

os.makedirs(output_dir, exist_ok=True)
with open(out_path, "w") as f:
    json.dump(result, f, indent=2)

print(f"  ✓ {len(segments)} segments → {out_path}")
