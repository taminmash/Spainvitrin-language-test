"""Generate and send the next A1 lesson as a Telegram MP4 video.

Required environment variables for real sending:
- BOT_TOKEN: Telegram bot token.
- TELEGRAM_CHAT_ID: target chat/channel id, for example @channel_username.
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from gtts import gTTS
from playwright.async_api import async_playwright
from telegram import Bot


ROOT = Path(__file__).resolve().parent
DEFAULT_LEVEL = "a1"
DEFAULT_LESSONS_DIR = ROOT / "source_content"
DEFAULT_PROGRESS_PATH = ROOT / "progress" / "a1_daily.json"
MADRID_TZ = ZoneInfo("Europe/Madrid")

CARD_WIDTH = 900
CARD_HEIGHT = 1200

LOCAL_BROWSER_CANDIDATES = [
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
]

PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
UNIT_HEADING_RE = re.compile(r"^#\s+واحد\s+([۰-۹٠-٩\d]+)")
LESSON_HEADING_RE = re.compile(r"^###\s+درس\s+([۰-۹٠-٩\d]+)\s*(?:[—\-:：]\s*(.+))?\s*$")
BOLD_LABEL_RE = re.compile(r"^\*\*(.+?):\*\*\s*(.*)$")
SEPARATOR_RE = re.compile(r"^\s*-{3,}\s*$")


@dataclass
class LessonSection:
    label: str
    lines: list[str]


@dataclass
class MarkdownLesson:
    lesson_number: int
    unit_number: int
    lesson_type: str
    heading_title: str
    fields: dict[str, str]
    sections: list[LessonSection]
    source_file: str
    source_line: int


class SenderError(Exception):
    """Raised when a daily lesson cannot be generated or sent."""


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def escape(value: object) -> str:
    return html.escape(str(value or ""))


def normalize_digits(value: str) -> str:
    return value.translate(PERSIAN_DIGITS)


def parse_int(value: str) -> int:
    match = re.search(r"\d+", normalize_digits(value))
    if not match:
        raise SenderError(f"Expected number in {value!r}")
    return int(match.group(0))


def clean_heading_text(text: str) -> str:
    return re.sub(r"^[^\w\u0600-\u06FF]+", "", text.strip()).strip()


def parse_heading_type_and_title(heading_text: str) -> tuple[str, str]:
    cleaned = clean_heading_text(heading_text)
    if ":" in cleaned:
        lesson_type, title = cleaned.split(":", 1)
    elif "：" in cleaned:
        lesson_type, title = cleaned.split("：", 1)
    else:
        parts = cleaned.split(maxsplit=1)
        lesson_type = parts[0] if parts else "lesson"
        title = parts[1] if len(parts) > 1 else cleaned
    return lesson_type.strip() or "lesson", title.strip() or cleaned


def source_label_key(label: str) -> str:
    mapping = {
        "کلمه اصلی": "main_text",
        "مصدر": "main_text",
        "جمله اصلی": "main_text",
        "موضوع اصلی": "main_text",
        "موضوع": "main_text",
        "تلفظ": "pronunciation",
        "معنی": "meaning_fa",
        "انگلیسی": "meaning_en",
        "صدا": "audio_text",
    }
    return mapping.get(label.strip(), "")


def split_lesson_sections(lines: list[str]) -> tuple[dict[str, str], list[LessonSection]]:
    fields: dict[str, str] = {}
    sections: list[LessonSection] = []
    current: LessonSection | None = None

    for raw_line in lines:
        line = raw_line.rstrip()
        if not line.strip():
            if current is not None:
                current.lines.append("")
            continue

        label_match = BOLD_LABEL_RE.match(line.strip())
        if label_match:
            label = label_match.group(1).strip()
            value = label_match.group(2).strip()
            field_key = source_label_key(label)
            if field_key:
                fields[field_key] = value
                current = None
            else:
                current = LessonSection(label=label, lines=[])
                if value:
                    current.lines.append(value)
                sections.append(current)
            continue

        if current is not None:
            current.lines.append(line)

    return fields, sections


def load_lessons(level: str, lessons_dir: Path) -> list[MarkdownLesson]:
    level_dir = lessons_dir / level
    if not level_dir.exists():
        raise SenderError(f"Source content folder not found: {level_dir}")

    lessons: list[MarkdownLesson] = []
    for markdown_path in sorted(level_dir.glob("*.md")):
        text = markdown_path.read_text(encoding="utf-8-sig")
        lines = text.splitlines()
        unit_number = 0
        active: dict[str, object] | None = None

        def finish_active() -> None:
            nonlocal active
            if active is None:
                return
            lesson_type, heading_title = parse_heading_type_and_title(str(active["heading_text"]))
            fields, sections = split_lesson_sections(active["lines"])
            lessons.append(
                MarkdownLesson(
                    lesson_number=int(active["lesson_number"]),
                    unit_number=int(active["unit_number"]),
                    lesson_type=lesson_type,
                    heading_title=heading_title,
                    fields=fields,
                    sections=sections,
                    source_file=markdown_path.relative_to(ROOT).as_posix(),
                    source_line=int(active["source_line"]),
                )
            )
            active = None

        for line_number, line in enumerate(lines, start=1):
            unit_match = UNIT_HEADING_RE.match(line)
            if unit_match:
                unit_number = parse_int(unit_match.group(1))
                continue

            lesson_match = LESSON_HEADING_RE.match(line)
            if lesson_match:
                finish_active()
                active = {
                    "lesson_number": parse_int(lesson_match.group(1)),
                    "unit_number": unit_number,
                    "heading_text": lesson_match.group(2) or "",
                    "source_line": line_number,
                    "lines": [],
                }
                continue

            if active is not None and not SEPARATOR_RE.match(line):
                active["lines"].append(line)

        finish_active()

    if not lessons:
        raise SenderError(f"No markdown lessons found in {level_dir}")

    return sorted(lessons, key=lambda item: item.lesson_number)


def load_progress(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"level": DEFAULT_LEVEL, "last_published_lesson": 0, "history": []}
    return json.loads(path.read_text(encoding="utf-8"))


def save_progress(path: Path, progress: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(progress, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def next_lesson(lessons: list[MarkdownLesson], progress: dict[str, object]) -> MarkdownLesson | None:
    last_published = int(progress.get("last_published_lesson", 0))
    for lesson in lessons:
        if lesson.lesson_number > last_published:
            return lesson
    return None


def lesson_type_badge(lesson_type: object) -> str:
    lesson_type_text = str(lesson_type or "")
    icons = {
        "واژه": "📚",
        "فعل": "🔤",
        "مکالمه": "🗣️",
        "گرامر": "📝",
        "مرور": "🔄",
    }
    return f"{icons.get(lesson_type_text, '📘')} {lesson_type_text}".strip()


def is_table_separator(line: str) -> bool:
    stripped = line.strip().strip("|")
    return bool(stripped) and all(part.strip().replace("-", "").replace(":", "") == "" for part in stripped.split("|"))


def parse_markdown_table(lines: list[str]) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("|") or is_table_separator(stripped):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        rows.append(cells)
    return rows


def table_html(rows: list[list[str]], *, class_name: str = "content-table") -> str:
    if not rows:
        return ""
    header = rows[0]
    body_rows = rows[1:]
    head_html = "".join(f"<th>{escape(cell)}</th>" for cell in header)
    body_html = "".join(
        "<tr>" + "".join(f"<td>{escape(cell)}</td>" for cell in row) + "</tr>"
        for row in body_rows
    )
    return f"<table class='{class_name}'><thead><tr>{head_html}</tr></thead><tbody>{body_html}</tbody></table>"


def render_section_lines(lines: list[str]) -> str:
    chunks: list[str] = []
    paragraph: list[str] = []
    table_lines: list[str] = []

    def flush_paragraph() -> None:
        nonlocal paragraph
        if not paragraph:
            return
        bullet_lines = [line.lstrip("-").strip() for line in paragraph if line.strip().startswith("-")]
        if bullet_lines and len(bullet_lines) == len([line for line in paragraph if line.strip()]):
            chunks.append("<ul>" + "".join(f"<li>{escape(line)}</li>" for line in bullet_lines) + "</ul>")
        else:
            text = "<br>".join(escape(line.strip()) for line in paragraph if line.strip())
            if text:
                chunks.append(f"<p>{text}</p>")
        paragraph = []

    def flush_table() -> None:
        nonlocal table_lines
        if table_lines:
            chunks.append(table_html(parse_markdown_table(table_lines)))
            table_lines = []

    for line in lines:
        if line.strip().startswith("|"):
            flush_paragraph()
            table_lines.append(line)
        else:
            flush_table()
            paragraph.append(line)

    flush_paragraph()
    flush_table()
    return "".join(chunks)


def section_by_label(lesson: MarkdownLesson, labels: set[str]) -> LessonSection | None:
    for section in lesson.sections:
        if section.label in labels:
            return section
    return None


def usage_line_html(lesson: MarkdownLesson) -> str:
    section = section_by_label(lesson, {"🎯 کاربرد", "کاربرد"})
    if section is None:
        return ""
    return f'<div class="usage-line">🎯 {render_section_lines(section.lines)}</div>'


def english_note_html(lesson: MarkdownLesson) -> str:
    english = str(lesson.fields.get("meaning_en", "")).strip()
    if not english:
        return ""
    return f'<p class="english-note" dir="ltr">{escape(english)}</p>'


def conversation_bubbles_html(lesson: MarkdownLesson) -> str:
    section = section_by_label(lesson, {"مکالمه کامل"})
    if section is None:
        return ""

    rows = parse_markdown_table(section.lines)
    if len(rows) < 2:
        return ""
    bubbles: list[str] = []
    for row in rows[1:]:
        if len(row) < 4:
            continue
        speaker, spanish, persian, english = row[:4]
        bubbles.append(
            f"""
            <div class="chat-bubble">
              <div class="speaker">{escape(speaker)}</div>
              <div class="chat-es" dir="ltr">{escape(spanish)}</div>
              <div class="chat-fa">{escape(persian)}</div>
              <div class="chat-en" dir="ltr">{escape(english)}</div>
            </div>
            """
        )
    if not bubbles:
        return ""
    return f'<section class="chat-section">{"".join(bubbles)}</section>'


def content_sections_html(lesson: MarkdownLesson) -> str:
    excluded = {"نکته گرامری", "نکات گرامری", "🎯 کاربرد", "کاربرد", "مکالمه کامل"}
    blocks: list[str] = []
    for section in lesson.sections:
        if section.label in excluded:
            continue
        rendered = render_section_lines(section.lines)
        if rendered:
            blocks.append(f"<section class='mini-section'><h3>{escape(section.label)}</h3>{rendered}</section>")
    return "".join(blocks)


def build_card_html(lesson: MarkdownLesson) -> str:
    title_es = lesson.fields.get("main_text") or lesson.heading_title
    title_en = lesson.fields.get("meaning_en", "")
    lesson_number = lesson.lesson_number
    unit_number = lesson.unit_number

    title_en_block = f"<p class='title-en' dir='ltr'>{escape(title_en)}</p>" if title_en else ""
    grammar_section = section_by_label(lesson, {"نکته گرامری", "نکات گرامری"})
    grammar_block = render_section_lines(grammar_section.lines) if grammar_section else ""
    usage_block = usage_line_html(lesson)
    conversation_block = conversation_bubbles_html(lesson)
    content_blocks = content_sections_html(lesson)

    return f"""<!doctype html>
<html lang="fa" dir="rtl">
<head>
  <meta charset="utf-8">
  <style>
    * {{ box-sizing: border-box; }}
    html, body {{
      width: {CARD_WIDTH}px;
      height: {CARD_HEIGHT}px;
      margin: 0;
      overflow: hidden;
      background: #1a0f0a;
      color: #fff7ed;
      font-family: Vazirmatn, Tahoma, Arial, sans-serif;
    }}
    .stage {{
      width: {CARD_WIDTH}px;
      height: {CARD_HEIGHT}px;
      display: flex;
      align-items: center;
      justify-content: center;
      background: #1a0f0a;
    }}
    .card {{
      position: relative;
      width: 820px;
      height: 1120px;
      overflow: hidden;
      border-radius: 48px;
      padding: 42px 48px 36px;
      background: linear-gradient(155deg, #c0392b 0%, #96281B 35%, #1a1a2e 100%);
      box-shadow: 0 20px 60px rgba(0,0,0,0.6);
    }}
    .strip {{
      position: absolute;
      inset: 0 0 auto 0;
      height: 7px;
      background: #c0392b;
    }}
    .header {{
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      direction: ltr;
    }}
    .badges {{
      display: flex;
      gap: 12px;
      direction: rtl;
    }}
    .badge {{
      border: 1px solid rgba(255,255,255,0.22);
      border-radius: 999px;
      padding: 10px 18px;
      background: rgba(26,15,10,0.42);
      color: #fff;
      font-size: 24px;
      font-weight: 700;
      line-height: 1;
      white-space: nowrap;
    }}
    .flag {{
      font-size: 46px;
      line-height: 1;
    }}
    .hero {{
      margin-top: 48px;
    }}
    .spanish-row {{
      display: flex;
      align-items: flex-start;
      gap: 18px;
      direction: ltr;
    }}
    .title-es {{
      max-width: 520px;
      margin: 0;
      color: #fff;
      font-family: "Playfair Display", Georgia, "Times New Roman", serif;
      font-size: 76px;
      font-weight: 700;
      line-height: 0.98;
      text-align: left;
      direction: ltr;
    }}
    .type {{
      margin-top: 10px;
      border: 1px solid rgba(255,255,255,0.18);
      border-radius: 999px;
      padding: 10px 16px;
      background: rgba(255,255,255,0.14);
      color: #fff7ed;
      font-size: 24px;
      font-weight: 700;
      white-space: nowrap;
    }}
    .title-en {{
      margin: 16px 0 0;
      color: rgba(255,255,255,0.62);
      font-family: Inter, "Segoe UI", Arial, sans-serif;
      font-size: 29px;
      line-height: 1.25;
      text-align: left;
    }}
    .pronunciation {{
      margin: 36px 0 0;
      color: #ffd166;
      font-size: 34px;
      font-weight: 500;
      line-height: 1.2;
      text-align: right;
    }}
    .meaning-fa {{
      margin: 14px 0 0;
      color: #fff7ed;
      font-size: 32px;
      font-weight: 800;
      line-height: 1.25;
      text-align: right;
    }}
    .box {{
      margin-top: 22px;
      border-radius: 28px;
      border: 1px solid rgba(255,255,255,0.18);
      background: rgba(12,14,30,0.58);
      padding: 22px 26px;
      color: #fff7ed;
    }}
    .box h2 {{
      margin: 0 0 10px;
      color: #ffd166;
      font-size: 24px;
      line-height: 1.2;
    }}
    ul {{
      margin: 0;
      padding: 0 28px 0 0;
    }}
    li {{
      margin: 5px 0;
      font-size: 20px;
      line-height: 1.35;
    }}
    .usage-line {{
      margin: 22px 0 0;
      color: #ffd166;
      font-size: 21px;
      font-weight: 700;
      line-height: 1.45;
      text-align: right;
    }}
    .english-note {{
      margin: 18px 0 0;
      color: rgba(255,255,255,0.56);
      font-family: Inter, "Segoe UI", Arial, sans-serif;
      font-size: 20px;
      line-height: 1.35;
      text-align: left;
    }}
      .chat-section {{
      margin-top: 20px;
      display: grid;
      gap: 10px;
    }}
    .chat-bubble {{
      border-radius: 26px;
      border: 1px solid rgba(255,255,255,0.16);
      background: rgba(12,14,30,0.50);
      padding: 12px 18px;
      position: relative;
    }}
    .speaker {{
      position: absolute;
      top: 12px;
      right: 14px;
      font-size: 22px;
    }}
    .chat-es {{
      color: #fff;
      font-family: Inter, "Segoe UI", Arial, sans-serif;
      font-size: 19px;
      font-weight: 700;
      line-height: 1.25;
      text-align: left;
      padding-right: 42px;
    }}
    .chat-fa {{
      margin-top: 7px;
      color: #ffd166;
      font-size: 18px;
      line-height: 1.35;
      text-align: right;
    }}
    .chat-en {{
      margin-top: 5px;
      color: rgba(255,255,255,0.48);
      font-family: Inter, "Segoe UI", Arial, sans-serif;
      font-size: 16px;
      line-height: 1.25;
      text-align: left;
    }}
      .footer {{
      position: absolute;
      left: 54px;
      right: 54px;
      bottom: 38px;
      display: flex;
      justify-content: space-between;
      gap: 28px;
      direction: ltr;
      color: rgba(255,255,255,0.82);
      font-family: Inter, "Segoe UI", Arial, sans-serif;
      font-size: 21px;
      line-height: 1.55;
    }}
    .footer-left {{
      text-align: left;
    }}
    .footer-right {{
      text-align: right;
    }}
    .body-grid {{
      transform-origin: top center;
    }}
    .mini-section {{
      margin-top: 16px;
      border-radius: 22px;
      border: 1px solid rgba(255,255,255,0.14);
      background: rgba(12,14,30,0.40);
      padding: 14px 16px;
    }}
    .mini-section h3 {{
      margin: 0 0 8px;
      color: #ffd166;
      font-size: 21px;
      line-height: 1.2;
    }}
    .mini-section p,
    .box p {{
      margin: 0;
      font-size: 19px;
      line-height: 1.35;
    }}
    .content-table {{
      width: 100%;
      border-collapse: collapse;
      direction: rtl;
      font-size: 15px;
      line-height: 1.25;
    }}
    .content-table th,
    .content-table td {{
      border: 1px solid rgba(255,255,255,0.16);
      padding: 5px 7px;
      vertical-align: top;
    }}
    .content-table th {{
      color: #ffd166;
      font-weight: 800;
    }}
    .sound {{
      font-size: 52px;
      line-height: 1;
      margin-bottom: 8px;
    }}
    a {{
      color: inherit;
      text-decoration: none;
    }}
  </style>
</head>
<body>
  <main class="stage">
    <article class="card">
      <div class="strip"></div>
      <header class="header">
        <div class="badges">
          <span class="badge">A1 — واحد {unit_number}</span>
          <span class="badge">درس {lesson_number}</span>
        </div>
        <div class="flag">🇪🇸</div>
      </header>
      <section class="hero">
        <div class="spanish-row">
          <h1 class="title-es">{escape(title_es)}</h1>
          <span class="type">{escape(lesson_type_badge(lesson.lesson_type))}</span>
        </div>
        {title_en_block}
        <p class="pronunciation">{escape(lesson.fields.get("pronunciation"))}</p>
        <p class="meaning-fa">{escape(lesson.fields.get("meaning_fa"))}</p>
      </section>
      <div class="body-grid">
        {conversation_block}
        {content_blocks}
        <section class="box">
          <h2>نکته</h2>
          {grammar_block}
          {usage_block}
          {english_note_html(lesson)}
        </section>
      </div>
      <footer class="footer">
        <div class="footer-left">
          <div class="sound">🔊</div>
          <div>Design by: Tamin .M</div>
          <div>💬 @taminmashoori</div>
          <div>✉️ <a href="mailto:tamin.mashoori@gmail.com">tamin.mashoori@gmail.com</a></div>
        </div>
        <div class="footer-right">
          <div>📲 t.me/vitrinspain</div>
          <div>🌿 t.me/hayatkhalvatspain</div>
          <div>🤖 @VitrinSpainBot</div>
        </div>
      </footer>
    </article>
  </main>
</body>
</html>"""


async def create_card_image(lesson: MarkdownLesson, output_path: Path) -> None:
    html_content = build_card_html(lesson)
    async with async_playwright() as playwright:
        launch_options: dict[str, str] = {}
        browser_path = os.environ.get("CHROME_EXECUTABLE_PATH", "").strip()
        if not browser_path:
            browser_path = next((str(path) for path in LOCAL_BROWSER_CANDIDATES if path.exists()), "")
        if browser_path:
            launch_options["executable_path"] = browser_path
        browser = await playwright.chromium.launch(**launch_options)
        page = await browser.new_page(viewport={"width": CARD_WIDTH, "height": CARD_HEIGHT})
        await page.set_content(html_content, wait_until="networkidle")
        await page.evaluate(
            """
            () => {
              const body = document.querySelector('.body-grid');
              const footer = document.querySelector('.footer');
              if (!body || !footer) return;
              const bodyTop = body.getBoundingClientRect().top;
              const footerTop = footer.getBoundingClientRect().top;
              const available = Math.max(260, footerTop - bodyTop - 18);
              const needed = body.scrollHeight;
              if (needed > available) {
                const scale = Math.max(0.54, available / needed);
                body.style.transform = `scale(${scale})`;
                body.style.width = `${100 / scale}%`;
                body.style.marginInline = `${(100 - (100 / scale)) / 2}%`;
              }
            }
            """
        )
        await page.screenshot(
            path=str(output_path),
            clip={"x": 0, "y": 0, "width": CARD_WIDTH, "height": CARD_HEIGHT},
        )
        await browser.close()


def create_audio(text: str, output_path: Path) -> None:
    if not text.strip():
        raise SenderError("audio_text is empty; cannot create MP3.")
    tts = gTTS(text, lang="es", slow=True)
    tts.save(str(output_path))


def create_video(image_path: Path, audio_path: Path, output_path: Path) -> None:
    ffmpeg_path = os.environ.get("FFMPEG_PATH", "").strip() or shutil.which("ffmpeg") or "ffmpeg"
    subprocess.run(
        [
            ffmpeg_path,
            "-y",
            "-loop",
            "1",
            "-i",
            str(image_path),
            "-i",
            str(audio_path),
            "-c:v",
            "libx264",
            "-tune",
            "stillimage",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-pix_fmt",
            "yuv420p",
            "-shortest",
            str(output_path),
        ],
        check=True,
        capture_output=True,
    )


async def create_lesson_video(lesson: MarkdownLesson, work_dir: Path) -> Path:
    lesson_number = lesson.lesson_number
    image_path = work_dir / f"a1_lesson_{lesson_number:03d}.png"
    audio_path = work_dir / f"a1_lesson_{lesson_number:03d}.mp3"
    video_path = work_dir / f"a1_lesson_{lesson_number:03d}.mp4"

    await create_card_image(lesson, image_path)
    create_audio(lesson.fields.get("audio_text", ""), audio_path)
    create_video(image_path, audio_path, video_path)
    return video_path


async def send_telegram_video(token: str, chat_id: str, video_path: Path) -> object:
    bot = Bot(token=token)
    with video_path.open("rb") as video:
        return await bot.send_video(chat_id=chat_id, video=video, supports_streaming=True)


def should_send_now(force: bool, now: datetime) -> bool:
    return force or now.astimezone(MADRID_TZ).hour == 8


async def publish_next_lesson(
    *,
    level: str,
    lessons_dir: Path,
    progress_path: Path,
    dry_run: bool,
    force: bool,
    keep_artifacts: bool,
) -> int:
    now = datetime.now(tz=MADRID_TZ)
    if not should_send_now(force, now):
        print(f"Skipping: Madrid time is {now:%Y-%m-%d %H:%M}, not 08:00.")
        return 0

    lessons = load_lessons(level, lessons_dir)
    progress = load_progress(progress_path)
    lesson = next_lesson(lessons, progress)
    if lesson is None:
        print("No remaining lessons to publish.")
        return 0

    lesson_number = lesson.lesson_number
    with tempfile.TemporaryDirectory(prefix=f"{level}_lesson_{lesson_number:03d}_") as tmp:
        work_dir = Path(tmp)
        video_path = await create_lesson_video(lesson, work_dir)
        print(f"Generated MP4 for lesson {lesson_number}: {video_path}")

        if dry_run:
            if keep_artifacts:
                kept_path = ROOT / "tmp" / "dry_run" / f"{level}_lesson_{lesson_number:03d}.mp4"
                kept_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(video_path, kept_path)
                print(f"Dry run: kept MP4 at {kept_path}")
            else:
                print("Dry run: Telegram send skipped; progress unchanged.")
            return 0

        token = os.environ.get("BOT_TOKEN", "").strip()
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", os.environ.get("TELEGRAM_CHANNEL_ID", "")).strip()
        if not token:
            raise SenderError("BOT_TOKEN environment variable is required.")
        if not chat_id:
            raise SenderError("TELEGRAM_CHAT_ID environment variable is required.")

        message = await send_telegram_video(token, chat_id, video_path)
        message_id = getattr(message, "message_id", None)

    history = progress.setdefault("history", [])
    if isinstance(history, list):
        history.append(
            {
                "lesson_number": lesson_number,
                "unit_number": lesson.unit_number,
                "published_at_madrid": now.isoformat(),
                "telegram_message_id": message_id,
                "format": "mp4",
            }
        )

    progress["level"] = level
    progress["last_published_lesson"] = lesson_number
    progress["updated_at_madrid"] = now.isoformat()
    save_progress(progress_path, progress)

    print(f"Published lesson {lesson_number} as MP4; progress saved to {progress_path}.")
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Publish the next A1 lesson to Telegram as MP4.")
    parser.add_argument("--level", default=DEFAULT_LEVEL)
    parser.add_argument("--lessons-dir", type=Path, default=DEFAULT_LESSONS_DIR)
    parser.add_argument("--progress-path", type=Path, default=DEFAULT_PROGRESS_PATH)
    parser.add_argument("--dry-run", action="store_true", help="Generate one MP4 but do not send or update progress.")
    parser.add_argument("--force", action="store_true", help="Bypass the 08:00 Europe/Madrid time check.")
    parser.add_argument("--keep-artifacts", action="store_true", help="Keep the dry-run MP4 under tmp/dry_run/.")
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    args = build_arg_parser().parse_args(argv)
    try:
        return asyncio.run(
            publish_next_lesson(
                level=args.level.lower(),
                lessons_dir=args.lessons_dir,
                progress_path=args.progress_path,
                dry_run=args.dry_run,
                force=args.force,
                keep_artifacts=args.keep_artifacts,
            )
        )
    except SenderError as exc:
        print(f"telegram_daily_sender error: {exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr
        print(f"ffmpeg error: {stderr}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
