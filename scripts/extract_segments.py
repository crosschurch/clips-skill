#!/usr/bin/env python3

import json
import subprocess
import sys
import os
from pathlib import Path

def get_chapters(video_file):
    """Extract chapter information from video file using ffprobe."""
    cmd = [
        'ffprobe',
        '-v', 'quiet',
        '-print_format', 'json',
        '-show_chapters',
        video_file
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Error running ffprobe: {result.stderr}")
        return []
    
    data = json.loads(result.stdout)
    return data.get('chapters', [])

def format_time(seconds):
    """Convert seconds to HH:MM:SS format."""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"

def extract_segment(video_file, start_time, duration, output_file, chapter_name):
    """Extract a segment from the video using ffmpeg."""
    print(f"Extracting segment '{chapter_name}' to {output_file}")
    print(f"  Start: {format_time(start_time)}, Duration: {format_time(duration)}")
    
    cmd = [
        'ffmpeg',
        '-i', video_file,
        '-ss', str(start_time),
        '-t', str(duration),
        '-c', 'copy',  # Copy codecs without re-encoding for speed
        '-avoid_negative_ts', 'make_zero',
        '-y',  # Overwrite output files
        output_file
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Error extracting segment: {result.stderr}")
        return False
    return True

def main(video_file):
    """Main function to extract segments around OBS markers."""
    if not os.path.exists(video_file):
        print(f"Video file not found: {video_file}")
        sys.exit(1)
    
    # Get video duration using ffprobe
    cmd = ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_format', video_file]
    result = subprocess.run(cmd, capture_output=True, text=True)
    video_data = json.loads(result.stdout)
    video_duration = float(video_data['format']['duration'])
    
    # Get chapters/markers
    chapters = get_chapters(video_file)
    
    if not chapters:
        print("No chapters/markers found in the video")
        sys.exit(1)
    
    print(f"Found {len(chapters)} markers in the video")
    print(f"Video duration: {format_time(video_duration)}\n")
    
    # Process each chapter (skip the first "Start" chapter as it's at 0:00)
    base_name = Path(video_file).stem
    
    for i, chapter in enumerate(chapters):
        chapter_start = float(chapter['start_time'])
        chapter_name = chapter['tags']['title']
        
        # Skip the first "Start" marker at 0:00
        if i == 0 and chapter_start == 0:
            print(f"Skipping marker '{chapter_name}' at start of video\n")
            continue
        
        # Calculate segment boundaries
        # 3 minutes before = 180 seconds
        # 1 minute after = 60 seconds
        segment_start = max(0, chapter_start - 180)  # Don't go before video start
        segment_end = min(video_duration, chapter_start + 60)  # Don't go past video end
        segment_duration = segment_end - segment_start
        
        # Generate output filename
        safe_name = chapter_name.replace(' ', '_').replace('/', '-')
        time_str = format_time(chapter_start).replace(':', '-')
        output_file = f"{base_name}_marker_{i:02d}_{safe_name}_{time_str}.mp4"
        
        print(f"Marker {i}: '{chapter_name}' at {format_time(chapter_start)}")
        
        # Extract the segment
        success = extract_segment(video_file, segment_start, segment_duration, output_file, chapter_name)
        
        if success:
            print(f"  ✓ Successfully extracted to {output_file}\n")
        else:
            print(f"  ✗ Failed to extract segment\n")

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python extract_segments.py <video_file>")
        sys.exit(1)
    
    main(sys.argv[1])