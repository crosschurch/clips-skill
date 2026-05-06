#!/bin/bash
# Transcribe all marker clips in the current directory using Whisper.
# Skips clips that already have a transcript.
#
# Usage: bash transcribe.sh [work_dir]
#   work_dir defaults to CWD

set -euo pipefail

WORK_DIR="${1:-$(pwd)}"
TRANSCRIPTS_DIR="$WORK_DIR/transcripts"
mkdir -p "$TRANSCRIPTS_DIR"

cd "$WORK_DIR"

shopt -s nullglob
CLIPS=(*_marker_*.mp4)
shopt -u nullglob

if [ ${#CLIPS[@]} -eq 0 ]; then
    echo "No *_marker_*.mp4 clips found in: $WORK_DIR"
    echo "Run extract_segments.py first to generate marker clips."
    exit 1
fi

echo "Found ${#CLIPS[@]} marker clip(s) in: $WORK_DIR"
echo "Transcripts dir: $TRANSCRIPTS_DIR"
echo ""

for clip in "${CLIPS[@]}"; do
    stem="${clip%.mp4}"
    transcript="$TRANSCRIPTS_DIR/${stem}.json"

    if [ -f "$transcript" ]; then
        echo "✓ Already transcribed: $clip"
        continue
    fi

    echo "▶ Transcribing: $clip"
    whisper "$clip" \
        --model medium \
        --language en \
        --output_format json \
        --output_dir "$TRANSCRIPTS_DIR" \
        --word_timestamps True \
        --verbose False

    if [ $? -eq 0 ]; then
        echo "  ✓ Done"
    else
        echo "  ✗ Failed — whisper exited with error"
    fi
    echo ""
done

echo "Transcription complete."
echo "Transcripts saved to: $TRANSCRIPTS_DIR"

# ── Full sermon ──────────────────────────────────────────────────────────────
# Detect: any MP4 >10 minutes that is not a marker clip or OBS recording
FULL_VIDEO=""
MAX_DUR=0
shopt -s nullglob
for f in *.mp4; do
    [[ "$f" =~ _marker_ ]] && continue
    [[ "$f" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2} ]] && continue
    dur=$(ffprobe -v quiet -show_entries format=duration -of csv=p=0 "$f" 2>/dev/null | cut -d. -f1 || echo 0)
    (( dur > 600 && dur > MAX_DUR )) && { MAX_DUR="$dur"; FULL_VIDEO="$f"; }
done
shopt -u nullglob

if [ -z "$FULL_VIDEO" ]; then
    echo ""
    echo "No full sermon video found (no non-marker MP4 >10min)."
else
    STEM="${FULL_VIDEO%.mp4}"
    TRANSCRIPT="$TRANSCRIPTS_DIR/${STEM}.json"

    if [ -f "$TRANSCRIPT" ]; then
        echo ""
        echo "✓ Already transcribed: $FULL_VIDEO"
    else
        # Prefer a matching audio file — same duration ±120s, much faster to transcribe
        SOURCE="$FULL_VIDEO"
        shopt -s nullglob
        for ext in mp3 m4a aac wav; do
            for af in *."$ext"; do
                [ -f "$af" ] || continue
                adur=$(ffprobe -v quiet -show_entries format=duration -of csv=p=0 "$af" 2>/dev/null | cut -d. -f1 || echo 0)
                diff=$(( adur > MAX_DUR ? adur - MAX_DUR : MAX_DUR - adur ))
                if (( diff < 120 )); then
                    SOURCE="$af"
                    break 2
                fi
            done
        done
        shopt -u nullglob

        echo ""
        echo "▶ Transcribing full sermon: $FULL_VIDEO"
        [ "$SOURCE" != "$FULL_VIDEO" ] && echo "  (using audio: $SOURCE)"

        # Use faster-whisper (int8 CPU mode) — 3-4x faster than standard whisper on CPU.
        # Falls back to standard whisper if faster-whisper is not available.
        FW_SCRIPT="$(dirname "$0")/transcribe_faster.py"
        if python3 -c "import faster_whisper" 2>/dev/null && [ -f "$FW_SCRIPT" ]; then
            python3 "$FW_SCRIPT" "$SOURCE" "$TRANSCRIPTS_DIR" "$STEM"
        else
            whisper "$SOURCE" \
                --model medium \
                --language en \
                --output_format json \
                --output_dir "$TRANSCRIPTS_DIR" \
                --word_timestamps True \
                --verbose False
            # If we transcribed the audio, rename the output to match the video stem
            SOURCE_STEM="${SOURCE%.*}"
            if [ "$SOURCE_STEM" != "$STEM" ] && [ -f "$TRANSCRIPTS_DIR/${SOURCE_STEM}.json" ]; then
                mv "$TRANSCRIPTS_DIR/${SOURCE_STEM}.json" "$TRANSCRIPT"
                echo "  (renamed transcript → ${STEM}.json)"
            fi
        fi

        if [ -f "$TRANSCRIPT" ]; then
            echo "  ✓ Done: $FULL_VIDEO"
        else
            echo "  ✗ Transcript not found at expected path — check output above"
        fi
    fi
fi
