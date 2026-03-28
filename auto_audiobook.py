import os
import re
import json
import uuid
import urllib.request
import urllib.parse
import subprocess
import argparse
import websocket # pip install websocket-client
from bs4 import BeautifulSoup # pip install beautifulsoup4
import ebooklib
from ebooklib import epub

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_INPUT_BOOK = os.path.join(SCRIPT_DIR, "pg35-images-3.epub")
DEFAULT_OUTPUT_DIR = os.path.join(SCRIPT_DIR, "audiobook_output")
REFERENCE_VOICE = "vw.wav"  # Must exist in ComfyUI/input/ folder
COMFYUI_SERVER_ADDRESS = "127.0.0.1:8188"
CLIENT_ID = str(uuid.uuid4())

# ================= WORKFLOW TEMPLATE =================

WORKFLOW_TEMPLATE = {
    "15": {
        "inputs": {
            "audio": REFERENCE_VOICE,
            "audioUI": ""
        },
        "class_type": "LoadAudio",
        "_meta": {"title": "LoadAudio"}
    },
    "16": {
        "inputs": {
            "filename_prefix": "AutoAudiobook_Segment",
            "audio": ["44", 0]
        },
        "class_type": "SaveAudio",
        "_meta": {"title": "Save Audio"}
    },
    "44": {
        "inputs": {
            "text": "",
            "model": "VibeVoice-1.5B",
            "attention_type": "auto",
            "quantize_llm": "full precision",
            "free_memory_after_generate": False,
            "diffusion_steps": 25,
            "seed": 0,
            "cfg_scale": 1.3,
            "use_sampling": False,
            "temperature": 0.95,
            "top_p": 0.95,
            "max_words_per_chunk": 250,
            "voice_speed_factor": 1,
            "voice_to_clone": ["15", 0]
        },
        "class_type": "VibeVoiceSingleSpeakerNode",
        "_meta": {"title": "VibeVoice Single Speaker"}
    }
}

# ================= HELPERS =================

def extract_text_blocks_from_epub(epub_path):
    """Return a list of (title, text) blocks in spine order."""
    if not os.path.exists(epub_path):
        print(f"ERROR: File not found: {epub_path}")
        return []

    book = epub.read_epub(epub_path)
    blocks = []

    for spine_item in book.spine:
        item_id = spine_item[0] if isinstance(spine_item, tuple) else spine_item
        item = book.get_item_with_id(item_id)
        if not item or item.get_type() != ebooklib.ITEM_DOCUMENT:
            continue

        soup = BeautifulSoup(item.get_body_content(), "html.parser")
        for script in soup(["script", "style"]):
            script.extract()

        text = soup.get_text(separator="\n")
        lines = (line.strip() for line in text.splitlines())
        chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
        clean_text = " ".join(chunk for chunk in chunks if chunk)

        title_tag = soup.find("h1") or soup.find("h2") or soup.find("title")
        title = title_tag.get_text().strip() if title_tag else item.get_id()

        if len(clean_text) > 50:
            blocks.append((title, clean_text))

    return blocks


def extract_text_blocks_from_text_file(text_path):
    """Read a plain text / markdown file and split it into paragraph blocks."""
    if not os.path.exists(text_path):
        print(f"ERROR: File not found: {text_path}")
        return []

    with open(text_path, "r", encoding="utf-8", errors="ignore") as f:
        raw = f.read()

    raw = raw.replace("\r\n", "\n").replace("\r", "\n")

    # Split on blank lines, normalize whitespace inside each paragraph.
    paragraphs = [
        re.sub(r"\s+", " ", p).strip()
        for p in re.split(r"\n\s*\n+", raw)
        if p.strip()
    ]

    blocks = []
    for i, para in enumerate(paragraphs):
        if len(para) > 20:
            blocks.append((f"Paragraph {i + 1}", para))

    return blocks


def group_blocks_into_chapters(blocks, pages_per_chapter):
    """Combine N EPUB blocks into one chapter."""
    if pages_per_chapter < 1:
        pages_per_chapter = 1

    chapters = []
    for i in range(0, len(blocks), pages_per_chapter):
        batch = blocks[i:i + pages_per_chapter]
        if not batch:
            continue

        first_title = batch[0][0]
        combined_text = " ".join(text for _, text in batch).strip()
        chapter_num = len(chapters) + 1
        chapter_title = f"Chapter {chapter_num}: {first_title}"
        chapters.append((chapter_title, combined_text))

    return chapters


def group_paragraphs_into_chapters(blocks, target_words_per_chapter=2500, min_paragraphs_per_chapter=3):
    """Group paragraph blocks into chapters using simple word-count detection."""
    if target_words_per_chapter < 1:
        target_words_per_chapter = 2500
    if min_paragraphs_per_chapter < 1:
        min_paragraphs_per_chapter = 1

    chapters = []
    current = []
    current_words = 0

    for title, text in blocks:
        words = len(text.split())

        should_cut = (
            current
            and current_words >= target_words_per_chapter
            and len(current) >= min_paragraphs_per_chapter
        )

        if should_cut:
            chapter_num = len(chapters) + 1
            combined = " ".join(t for _, t in current).strip()
            chapters.append((f"Chapter {chapter_num}", combined))
            current = []
            current_words = 0

        current.append((title, text))
        current_words += words

    if current:
        chapter_num = len(chapters) + 1
        combined = " ".join(t for _, t in current).strip()
        chapters.append((f"Chapter {chapter_num}", combined))

    return chapters


def split_text_smart(text, max_words=250):
    sentences = re.split(r'(?<=[.!?]) +', text)
    chunks = []
    current_chunk_str = ""

    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue

        word_count = len(sentence.split())
        if len(current_chunk_str.split()) + word_count <= max_words:
            current_chunk_str += " " + sentence
        else:
            if current_chunk_str.strip():
                chunks.append(current_chunk_str.strip())
            current_chunk_str = sentence

    if current_chunk_str.strip():
        chunks.append(current_chunk_str.strip())

    return chunks


def queue_prompt(prompt_workflow):
    p = {"prompt": prompt_workflow, "client_id": CLIENT_ID}
    data = json.dumps(p).encode("utf-8")
    req = urllib.request.Request(f"http://{COMFYUI_SERVER_ADDRESS}/prompt", data=data)
    return json.loads(urllib.request.urlopen(req).read())


def get_history(prompt_id):
    with urllib.request.urlopen(f"http://{COMFYUI_SERVER_ADDRESS}/history/{prompt_id}") as response:
        return json.loads(response.read())


def get_audio_data(filename, subfolder, folder_type):
    data = {"filename": filename, "subfolder": subfolder, "type": folder_type}
    url_values = urllib.parse.urlencode(data)
    with urllib.request.urlopen(f"http://{COMFYUI_SERVER_ADDRESS}/view?{url_values}") as response:
        return response.read()

def get_audio_duration_ms(file_path):
    """Use ffprobe to get the exact duration of an audio file in milliseconds."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration", "-of",
             "default=noprint_wrappers=1:nokey=1", file_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=True
        )
        return int(float(result.stdout.strip()) * 1000)
    except Exception as e:
        print(f"       [!] Warning: Could not get duration for {file_path} ({e})")
        return 0

def process_segment(text_segment):
    """Send text to ComfyUI and return (audio bytes, file extension)."""
    workflow = json.loads(json.dumps(WORKFLOW_TEMPLATE))
    workflow["44"]["inputs"]["text"] = text_segment

    try:
        ws = websocket.WebSocket()
        ws.connect(f"ws://{COMFYUI_SERVER_ADDRESS}/ws?clientId={CLIENT_ID}")

        prompt_response = queue_prompt(workflow)
        prompt_id = prompt_response["prompt_id"]

        while True:
            out = ws.recv()
            if isinstance(out, str):
                message = json.loads(out)
                if message.get("type") == "executing":
                    data = message.get("data", {})
                    if data.get("node") is None and data.get("prompt_id") == prompt_id:
                        break

        history = get_history(prompt_id)[prompt_id]
        outputs = history["outputs"]

        audio_content = None
        audio_ext = ".flac" 

        for node_id in outputs:
            node_output = outputs[node_id]
            if "audio" in node_output:
                for audio_file in node_output["audio"]:
                    audio_content = get_audio_data(
                        audio_file["filename"],
                        audio_file["subfolder"],
                        audio_file["type"]
                    )
                    _, ext = os.path.splitext(audio_file["filename"])
                    if ext:
                        audio_ext = ext.lower()

        ws.close()
        return audio_content, audio_ext

    except Exception as e:
        print(f"       [!] ComfyUI Communication Error: {e}")
        return None, None


def combine_audio_files(audio_files, output_filename, metadata=None, chapter_titles=None, cover_image=None):
    """Combine audio files and optionally embed chapter markers and cover art."""
    valid_files = [f for f in audio_files if os.path.exists(f)]
    if not valid_files:
        return False

    list_file = output_filename + ".concat.txt"
    meta_file = output_filename + ".ffmeta"
    
    try:
        with open(list_file, "w", encoding="utf-8") as f:
            for path in valid_files:
                f.write(f"file '{path.replace('\'', '\'\\\'\'')}'\n")

        # Base ffmpeg command
        cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_file]
        input_idx = 1 # Keep track of which input number we are on

        # 1. Add Chapters / Metadata
        if chapter_titles and len(chapter_titles) == len(valid_files):
            with open(meta_file, "w", encoding="utf-8") as m:
                m.write(";FFMETADATA1\n")
                if metadata:
                    for k, v in metadata.items():
                        if v: m.write(f"{k}={v}\n")
                
                current_ms = 0
                for i, path in enumerate(valid_files):
                    duration_ms = get_audio_duration_ms(path)
                    end_ms = current_ms + duration_ms
                    m.write(f"\n[CHAPTER]\nTIMEBASE=1/1000\nSTART={current_ms}\nEND={end_ms}\ntitle={chapter_titles[i]}\n")
                    current_ms = end_ms

            cmd.extend(["-i", meta_file, "-map_metadata", str(input_idx)])
            input_idx += 1
            
        elif metadata:
            for key, value in metadata.items():
                if value: cmd.extend(["-metadata", f"{key}={value}"])

        # 2. Add Cover Art (Mapped as a Video Stream)
        if cover_image and os.path.exists(cover_image):
            cmd.extend(["-i", cover_image])
            # Map audio from input 0, map video (cover) from the current input index
            cmd.extend(["-map", "0:a", "-map", f"{input_idx}:v"])
            cmd.extend(["-disposition:v", "attached_pic"])

        cmd.extend(["-c", "copy", output_filename])

        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception as e:
        print(f"       [!] Error during stitching: {e}")
        return False
    finally:
        try:
            if os.path.exists(list_file): os.remove(list_file)
            if os.path.exists(meta_file): os.remove(meta_file)
        except: pass

def safe_name(text):
    return "".join(c for c in text if c.isalpha() or c.isdigit() or c in (" ", "_", "-")).rstrip()


# ================= MAIN =================

def main():
    parser = argparse.ArgumentParser(description="Generate audiobook audio from EPUB or plain text using ComfyUI/VibeVoice.")
    parser.add_argument("--input-book", default=DEFAULT_INPUT_BOOK, help="Path to the input EPUB/TXT/MD file.")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help="Directory for generated audio.")
    parser.add_argument("--source-mode", choices=["auto", "epub", "text"], default="auto")
    parser.add_argument("--pages-per-chapter", type=int, default=1)
    parser.add_argument("--target-words-per-chapter", type=int, default=2500)
    parser.add_argument("--min-paragraphs-per-chapter", type=int, default=3)
    parser.add_argument("--chapters-per-part", type=int, default=5)
    parser.add_argument("--max-words-per-chunk", type=int, default=250)
    parser.add_argument("--diffusion-steps", type=int, default=25)
    parser.add_argument("--temperature", type=float, default=0.95)
    parser.add_argument("--top-p", type=float, default=0.95)
    parser.add_argument("--cfg-scale", type=float, default=1.3)
    parser.add_argument("--free-memory-after-generate", action="store_true")

    args = parser.parse_args()

    input_book = os.path.abspath(args.input_book)
    output_dir = os.path.abspath(args.output_dir)
    os.makedirs(output_dir, exist_ok=True)

    WORKFLOW_TEMPLATE["44"]["inputs"]["max_words_per_chunk"] = args.max_words_per_chunk
    WORKFLOW_TEMPLATE["44"]["inputs"]["diffusion_steps"] = args.diffusion_steps
    WORKFLOW_TEMPLATE["44"]["inputs"]["temperature"] = args.temperature
    WORKFLOW_TEMPLATE["44"]["inputs"]["top_p"] = args.top_p
    WORKFLOW_TEMPLATE["44"]["inputs"]["cfg_scale"] = args.cfg_scale
    WORKFLOW_TEMPLATE["44"]["inputs"]["free_memory_after_generate"] = args.free_memory_after_generate

    print(f"--- Processing Book: {input_book} ---")

    ext = os.path.splitext(input_book)[1].lower()
    
    # === METADATA EXTRACTION ATTEMPT ===
    global_album = os.path.splitext(os.path.basename(input_book))[0]
    global_artist = "Unknown"
    global_cover = None # <--- Add this
    
    if ext == ".epub":
        try:
            book = epub.read_epub(input_book)
            title_meta = book.get_metadata('DC', 'title')
            if title_meta: global_album = title_meta[0][0]
            creator_meta = book.get_metadata('DC', 'creator')
            if creator_meta: global_artist = creator_meta[0][0]
            print(f"   [Metadata] Found - Title: '{global_album}', Author: '{global_artist}'")
            
            # <--- Call our new function here
            global_cover = extract_cover_art(input_book, output_dir) 
        except Exception as e:
            print("   [Metadata] Could not read EPUB metadata, falling back to filename.")

    # === LOAD CHAPTERS ===
    if args.source_mode == "epub" or (args.source_mode == "auto" and ext == ".epub"):
        blocks = extract_text_blocks_from_epub(input_book)
        chapters = group_blocks_into_chapters(blocks, args.pages_per_chapter)
    elif args.source_mode == "text" or (args.source_mode == "auto" and ext in [".txt", ".md", ".markdown", ".rst"]):
        blocks = extract_text_blocks_from_text_file(input_book)
        chapters = group_paragraphs_into_chapters(
            blocks,
            target_words_per_chapter=args.target_words_per_chapter,
            min_paragraphs_per_chapter=args.min_paragraphs_per_chapter,
        )
    else:
        print("Unsupported input type. Use --source-mode epub or --source-mode text.")
        return

    if not chapters:
        print("No chapters found. Please check the input file or format.")
        return

    part_index = 1
    part_chapter_files = []

    for ch_idx, (title, text) in enumerate(chapters):
        print(f"\nProcessing {title}")

        if "Project Gutenberg" in text[:500]:
            print("   (Skipping likely Gutenberg preamble)")
            continue

        chunks = split_text_smart(text, max_words=args.max_words_per_chunk)
        print(f"   -> Split into {len(chunks)} segments.")

        segment_files = []

        for seg_idx, chunk in enumerate(chunks):
            if not chunk.strip():
                continue

            print(f"   -> Generating Segment {seg_idx + 1}/{len(chunks)}...", end="\r")
            audio_data, audio_ext = process_segment(chunk)

            if audio_data and len(audio_data) > 16:
                ext_to_use = audio_ext if audio_ext in [".wav", ".flac", ".mp3", ".opus"] else ".flac"
                temp_filename = os.path.join(output_dir, f"temp_ch{ch_idx + 1}_seg{seg_idx + 1}{ext_to_use}")
                with open(temp_filename, "wb") as f:
                    f.write(audio_data)
                segment_files.append(temp_filename)
                print(f"   -> Generated Segment {seg_idx + 1}/{len(chunks)} [OK]   ")
            else:
                print(f"   -> Generated Segment {seg_idx + 1}/{len(chunks)} [FAILED] - Invalid Audio Data")

        if not segment_files:
            print("   -> Chapter failed (no audio generated).")
            continue

        safe_title = safe_name(title) or f"Chapter_{ch_idx + 1:03d}"
        chapter_filename = os.path.join(output_dir, f"Chapter_{ch_idx + 1:03d}_{safe_title}.flac")

        # Apply Chapter Metadata
        chapter_meta = {
            "title": title,
            "artist": global_artist,
            "album": global_album,
            "track": str(ch_idx + 1)
        }

        print(f"   -> Stitching chapter to {chapter_filename}...")
        if combine_audio_files(segment_files, chapter_filename, metadata=chapter_meta):
            part_chapter_files.append((chapter_filename, title)) # <--- NOW SAVES THE TITLE TOO

        for f in segment_files:
            try:
                os.remove(f)
            except:
                pass

        print("   -> Chapter complete.")

        if len(part_chapter_files) >= args.chapters_per_part:
            part_filename = os.path.join(output_dir, f"{global_album} - Part_{part_index:03d}.flac")
            
            part_meta = {
                "title": f"{global_album} - Part {part_index}",
                "artist": global_artist,
                "album": global_album,
                "disc": str(part_index)
            }
            
            # Split the tuples back into files and titles
            files_to_stitch = [f[0] for f in part_chapter_files]
            titles_to_embed = [f[1] for f in part_chapter_files]
            
            print(f"   -> Stitching {len(part_chapter_files)} chapters into {part_filename}...")
            if combine_audio_files(files_to_stitch, part_filename, metadata=part_meta, chapter_titles=titles_to_embed):
                print(f"   -> Part {part_index:03d} complete.")
            part_index += 1
            part_chapter_files = []

    # Don't forget to do the exact same unpacking for the final leftover chapters outside the loop!
        if part_chapter_files:
            part_filename = os.path.join(output_dir, f"{global_album} - Part_{part_index:03d}.flac")
            part_meta = {
                "title": f"{global_album} - Part {part_index}",
                "artist": global_artist,
                "album": global_album,
                "disc": str(part_index)
            }
            
            files_to_stitch = [f[0] for f in part_chapter_files]
            titles_to_embed = [f[1] for f in part_chapter_files]
            
            print(f"   -> Stitching final {len(part_chapter_files)} chapters into {part_filename}...")
            if combine_audio_files(files_to_stitch, part_filename, metadata=part_meta, chapter_titles=titles_to_embed):
                print(f"   -> Part {part_index:03d} complete.")

    print("\nDone.")

def extract_cover_art(epub_path, output_dir):
    """Extract the cover image from an EPUB."""
    try:
        book = epub.read_epub(epub_path)
        cover_item = None
        
        # 1. Try official cover item flag
        for item in book.get_items():
            if item.get_type() == ebooklib.ITEM_COVER:
                cover_item = item
                break
        
        # 2. Fallback to guessing by filename
        if not cover_item:
            for item in book.get_items_of_type(ebooklib.ITEM_IMAGE):
                if 'cover' in item.get_name().lower():
                    cover_item = item
                    break
                    
        if cover_item:
            ext = os.path.splitext(cover_item.get_name())[1]
            if not ext: ext = ".jpg"
            cover_path = os.path.join(output_dir, f"cover{ext}")
            
            with open(cover_path, "wb") as f:
                f.write(cover_item.get_content())
            print(f"   [Cover Art] Extracted: {cover_path}")
            return cover_path
            
    except Exception as e:
        print(f"   [Cover Art] Could not extract cover: {e}")
        
    return None
if __name__ == "__main__":
    main()
