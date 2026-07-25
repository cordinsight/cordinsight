#!/usr/bin/env python3
"""Build docs/User-Guide.pdf and docs/User-Guide.docx from docs/User-Guide.md.

The User Guide mirrors the CordInsight lab-packet visual design (see
scripts/build_packet.py): a centered cover logo, maroon (#6C1D45) headings with
light-gray underlines, gold (#EAAA00) section rules, slate (#333F48)
sub-headings, gold-tinted table headers, and a clean sans body. Code blocks and
block quotes (which the Packet lacks) are styled to sit inside the same palette.

Fonts are macOS system faces: Arial Unicode (full glyph coverage incl. →, box
drawing) for body/bold and Courier New for monospace/ASCII diagrams. Requires:
    pip install markdown xhtml2pdf python-docx
Usage:
    python3 scripts/build_user_guide.py
"""
import re, shutil, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MD   = ROOT / "docs" / "User-Guide.md"
PDF  = ROOT / "docs" / "User-Guide.pdf"
DOCX = ROOT / "docs" / "User-Guide.docx"
LOGO = ROOT / "assets" / "Logo-CI-WhiteBG.jpeg"

MAROON = "#6C1D45"; GOLD = "#EAAA00"; SLATE = "#333F48"; GRAY = "#3a3a3a"

# --- fonts: copy system faces to space-free temp paths for xhtml2pdf ---
_FONT_SRC = {
    "regular": "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "bold":    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "mono":    "/System/Library/Fonts/Supplemental/Courier New.ttf",
    "italic":     "/System/Library/Fonts/Supplemental/Arial Italic.ttf",
    "bolditalic": "/System/Library/Fonts/Supplemental/Arial Bold Italic.ttf",
}
_tmp = Path(tempfile.gettempdir())
FONT_REG = _tmp / "cordinsight_ug_regular.ttf"
FONT_BLD = _tmp / "cordinsight_ug_bold.ttf"
FONT_MON = _tmp / "cordinsight_ug_mono.ttf"
FONT_ITAL = _tmp / "cordinsight_ug_italic.ttf"
FONT_BLDITAL = _tmp / "cordinsight_ug_bolditalic.ttf"
for dst, key in ((FONT_REG, "regular"), (FONT_BLD, "bold"), (FONT_MON, "mono"),
                 (FONT_ITAL, "italic"), (FONT_BLDITAL, "bolditalic")):
    src = Path(_FONT_SRC[key])
    if src.exists() and not dst.exists():
        shutil.copy(src, dst)


def _split_header(full_md: str):
    """Return (header_md, body_md) split at the first horizontal rule."""
    m = re.search(r"\n-{3,}\n", full_md)
    if not m:
        return "", full_md
    return full_md[: m.start()], full_md[m.end():]


def _header_fields(header_md: str):
    title = re.search(r"^#\s+(.*)$", header_md, flags=re.M)
    ver   = re.search(r"Version\s+([0-9][0-9A-Za-z.\-]*)", header_md)
    tag   = re.search(r"<em>(.*?)</em>", header_md)
    return (title.group(1).strip() if title else "CordInsight User Guide",
            ver.group(1) if ver else "",
            tag.group(1).strip() if tag else "")


def _ensure_list_blank_lines(body_md: str) -> str:
    """python-markdown needs a blank line between a paragraph and a following
    list; the source omits it in a few spots (e.g. a bold lead-in above bullets).
    Insert the blank line so lists render as bullets, not inline dashes. Code
    fences are left untouched."""
    out = []; in_fence = False
    list_re = re.compile(r'^\s*(?:[-*]|\d+\.)\s+\S')
    for ln in body_md.split("\n"):
        if ln.lstrip().startswith('```'):
            in_fence = not in_fence; out.append(ln); continue
        if not in_fence and list_re.match(ln) and out:
            prev = out[-1].strip()
            if prev and not list_re.match(out[-1]) and not prev.startswith(('|', '>', '#')):
                out.append("")
        out.append(ln)
    return "\n".join(out)


def build_pdf():
    import markdown
    from xhtml2pdf import pisa

    full = MD.read_text()
    header_md, body_md = _split_header(full)
    body_md = _ensure_list_blank_lines(body_md)
    title, ver, tag = _header_fields(header_md)

    body = markdown.markdown(body_md, extensions=['tables', 'fenced_code', 'sane_lists'])
    # section separators: xhtml2pdf draws <hr> black -> gold-filled div
    body = re.sub(r'<hr\s*/?>', '<div class="rule">&nbsp;</div>', body)

    cover = (
        f'<div class="cover">'
        f'<img class="logo" src="{LOGO}" alt="CordInsight Logo" />'
        f'<div class="title">{title}</div>'
        + (f'<div class="ver">Version {ver}</div>' if ver else '')
        + (f'<div class="tag">{tag}</div>' if tag else '')
        + f'<div class="rule">&nbsp;</div>'
        f'</div>'
    )

    css = f"""<style>
@font-face {{ font-family: S; src: url('{FONT_REG}'); }}
@font-face {{ font-family: S; src: url('{FONT_BLD}'); font-weight: bold; }}
@font-face {{ font-family: S; src: url('{FONT_ITAL}'); font-style: italic; }}
@font-face {{ font-family: S; src: url('{FONT_BLDITAL}'); font-weight: bold; font-style: italic; }}
@font-face {{ font-family: M; src: url('{FONT_MON}'); }}
@page {{ size: letter; margin: 2cm 2cm 2.2cm 2cm;
  @frame footer {{ -pdf-frame-content: footer; bottom: 1cm; height: 1cm; }} }}
body {{ font-family: S; font-size: 11pt; line-height: 1.4; color: {GRAY}; }}
.cover {{ text-align: center; }}
.logo {{ width: 230px; }}
.title {{ color: {MAROON}; font-size: 26pt; font-weight: bold; margin: 10px 0 4px; }}
.ver {{ color: {MAROON}; font-size: 13pt; font-weight: bold; margin: 2px 0; }}
.tag {{ color: {SLATE}; font-size: 12.5pt; font-style: italic; margin: 0 0 4px; }}
h1 {{ color: {MAROON}; font-size: 22pt; margin: 6px 0 2px; }}
h2 {{ color: {MAROON}; font-size: 17pt; margin: 18px 0 8px; padding-bottom: 4px;
     border-bottom: 1px solid #cfcfcf; }}
h3 {{ color: {SLATE}; font-size: 12.5pt; margin: 14px 0 4px; }}
h4 {{ color: {SLATE}; font-size: 11.5pt; margin: 10px 0 3px; }}
p {{ margin: 5px 0; }}
ul,ol {{ margin: 3px 0 8px 18px; }} li {{ margin: 2px 0; }}
ul {{ list-style-type: disc; }}
ul ul {{ list-style-type: circle; margin: 2px 0 4px 16px; }}
a {{ color: {MAROON}; text-decoration: none; }}
.rule {{ background-color: {GOLD}; height: 2px; line-height: 2px; font-size: 2px; margin: 14px 0; }}
table {{ border-collapse: collapse; margin: 8px 0; width: 100%; }}
th,td {{ border: 1px solid #bbb; padding: 4px 7px; font-size: 10pt; vertical-align: top; }}
th {{ background: #f3e7cd; color: {SLATE}; text-align: left; }}
code {{ font-family: M; font-size: 9.5pt; background: #f4f4f4; }}
pre {{ font-family: M; font-size: 8.5pt; background: #f4f4f4; color: {SLATE};
      padding: 6px; border: 1px solid #e2e2e2; }}
blockquote {{ margin: 8px 0; padding: 4px 10px; color: {SLATE};
      border-left: 3px solid {MAROON}; background: #f7f2f4; }}
</style>"""
    foot = ('<div id="footer" style="text-align:right; color:#888; font-size:9pt;">'
            '<pdf:pagenumber></div>')
    out = f"<html><head>{css}</head><body>{cover}{body}{foot}</body></html>"
    with open(PDF, "wb") as f:
        err = pisa.CreatePDF(out, dest=f, encoding='utf-8').err
    print(f"PDF  -> {PDF}  (err={err})")


def build_docx():
    from docx import Document
    from docx.shared import Pt, Inches, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement

    MRN = RGBColor(0x6C, 0x1D, 0x45); SLT = RGBColor(0x33, 0x3F, 0x48)
    GRY = RGBColor(0x3A, 0x3A, 0x3A)
    GLD = "EAAA00"; LGRAY = "CFCFCF"; LINEGRAY = "BBBBBB"; TH_BG = "F3E7CD"; CODE_BG = "F4F4F4"
    FONT = "Segoe UI"; MONO = "Courier New"

    doc = Document()
    st = doc.styles['Normal']; st.font.name = FONT; st.font.size = Pt(11); st.font.color.rgb = GRY
    doc.styles['Normal'].paragraph_format.space_after = Pt(6)
    for m in doc.sections:
        m.top_margin = m.bottom_margin = Inches(0.9); m.left_margin = m.right_margin = Inches(0.9)

    def bottom_border(p, color, sz):
        pPr = p._p.get_or_add_pPr(); pbdr = OxmlElement('w:pBdr'); b = OxmlElement('w:bottom')
        b.set(qn('w:val'), 'single'); b.set(qn('w:sz'), str(sz))
        b.set(qn('w:space'), '2'); b.set(qn('w:color'), color)
        pbdr.append(b); pPr.append(pbdr)

    def shade(el, color):
        pPr = el.get_or_add_tcPr() if el.tag.endswith('}tc') else el.get_or_add_pPr()
        shd = OxmlElement('w:shd'); shd.set(qn('w:val'), 'clear')
        shd.set(qn('w:color'), 'auto'); shd.set(qn('w:fill'), color)
        pPr.append(shd)

    INLINE = re.compile(r'(\*\*.+?\*\*|\*[^*]+?\*|`[^`]+?`|\[[^\]]+?\]\([^)]*?\))')

    def add_runs(p, text, bold_color=None, base_color=GRY):
        # Normalize inline HTML to markdown so <em>/<strong> render as italic/bold
        # in the DOCX (the PDF path goes through markdown and handles them already).
        text = re.sub(r'</?(strong|b)>', '**', text)
        text = re.sub(r'</?(em|i)>', '*', text)
        for seg in INLINE.split(text):
            if not seg:
                continue
            if seg.startswith('**') and seg.endswith('**'):
                r = p.add_run(seg[2:-2]); r.bold = True
                r.font.color.rgb = bold_color or base_color; r.font.name = FONT
            elif seg.startswith('*') and seg.endswith('*') and len(seg) > 2:
                r = p.add_run(seg[1:-1]); r.italic = True
                r.font.color.rgb = base_color; r.font.name = FONT
            elif seg.startswith('`') and seg.endswith('`'):
                r = p.add_run(seg[1:-1]); r.font.name = MONO; r.font.size = Pt(9.5)
                r.font.color.rgb = base_color
            elif seg.startswith('[') and '](' in seg:
                label = seg[1:seg.index(']')]
                r = p.add_run(label); r.font.color.rgb = MRN; r.font.name = FONT
            else:
                r = p.add_run(seg); r.font.color.rgb = base_color; r.font.name = FONT

    def heading(text, size, color, rule=False):
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(12); p.paragraph_format.space_after = Pt(4)
        r = p.add_run(text); r.bold = True; r.font.size = Pt(size)
        r.font.color.rgb = color; r.font.name = FONT
        if rule:
            bottom_border(p, LGRAY, 6)

    def split_row(line):
        cells = line.strip().strip('|').split('|')
        return [c.strip() for c in cells]

    def add_table(rows):
        header = split_row(rows[0])
        body = [split_row(r) for r in rows[2:]]
        ncol = len(header)
        t = doc.add_table(rows=1 + len(body), cols=ncol)
        t.style = 'Table Grid'
        for j, txt in enumerate(header):
            c = t.rows[0].cells[j]; shade(c._tc, TH_BG)
            c.paragraphs[0].text = ''
            add_runs(c.paragraphs[0], txt, bold_color=SLT, base_color=SLT)
            for r in c.paragraphs[0].runs:
                r.bold = True
        for i, row in enumerate(body, start=1):
            for j in range(ncol):
                c = t.rows[i].cells[j]
                c.paragraphs[0].text = ''
                add_runs(c.paragraphs[0], row[j] if j < len(row) else '')

    # ---- cover: logo + title + version + tagline ----
    full = MD.read_text()
    header_md, body_md = _split_header(full)
    title, ver, tag = _header_fields(header_md)
    if LOGO.exists():
        p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.add_run().add_picture(str(LOGO), width=Inches(2.6))
    p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_after = Pt(2)
    r = p.add_run(title); r.bold = True; r.font.size = Pt(24); r.font.color.rgb = MRN; r.font.name = FONT
    if ver:
        p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER; p.paragraph_format.space_after = Pt(0)
        r = p.add_run(f"Version {ver}"); r.bold = True; r.font.size = Pt(13); r.font.color.rgb = MRN; r.font.name = FONT
    if tag:
        p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER; p.paragraph_format.space_after = Pt(4)
        r = p.add_run(tag); r.italic = True; r.font.size = Pt(12.5); r.font.color.rgb = SLT; r.font.name = FONT
    p = doc.add_paragraph(); p.paragraph_format.space_before = Pt(2); p.paragraph_format.space_after = Pt(8)
    bottom_border(p, GLD, 18)

    # ---- body ----
    lines = body_md.split("\n"); i = 0; n = len(lines)
    while i < n:
        raw = lines[i]; s = raw.strip()
        # fenced code block
        if s.startswith('```'):
            i += 1; code = []
            while i < n and not lines[i].strip().startswith('```'):
                code.append(lines[i]); i += 1
            i += 1  # closing fence
            p = doc.add_paragraph(); p.paragraph_format.space_before = Pt(4); p.paragraph_format.space_after = Pt(6)
            shade(p._p, CODE_BG)
            for k, cl in enumerate(code):
                if k:
                    p.add_run().add_break()
                r = p.add_run(cl); r.font.name = MONO; r.font.size = Pt(8.5); r.font.color.rgb = SLT
            continue
        # table block
        if s.startswith('|') and i + 1 < n and re.match(r'^\s*\|?[\s:|-]+\|', lines[i + 1]) and '---' in lines[i + 1]:
            rows = []
            while i < n and lines[i].strip().startswith('|'):
                rows.append(lines[i]); i += 1
            add_table(rows); continue
        i += 1
        if s == "":
            continue
        if s == '---':
            p = doc.add_paragraph(); p.paragraph_format.space_before = Pt(2); p.paragraph_format.space_after = Pt(8)
            bottom_border(p, GLD, 18); continue
        if s.startswith('#### '):
            heading(s[5:], 11, SLT); continue
        if s.startswith('### '):
            heading(s[4:], 13, SLT); continue
        if s.startswith('## '):
            heading(s[3:], 16, MRN, rule=True); continue
        if s.startswith('# '):
            heading(s[2:], 20, MRN); continue
        if s.startswith('>'):
            p = doc.add_paragraph(); p.paragraph_format.left_indent = Inches(0.3)
            p.paragraph_format.space_after = Pt(4)
            pPr = p._p.get_or_add_pPr(); pbdr = OxmlElement('w:pBdr'); lb = OxmlElement('w:left')
            lb.set(qn('w:val'), 'single'); lb.set(qn('w:sz'), '18'); lb.set(qn('w:space'), '6'); lb.set(qn('w:color'), '6C1D45')
            pbdr.append(lb); pPr.append(pbdr)
            add_runs(p, s.lstrip('> ').rstrip(), base_color=SLT); continue
        mnum = re.match(r'^(\d+)\.\s+(.*)$', s)
        if mnum:
            p = doc.add_paragraph(style='List Number'); add_runs(p, mnum.group(2)); continue
        mb = re.match(r'^(\s*)[-*]\s+(.*)$', raw)
        if mb:
            indent = len(mb.group(1)); content = mb.group(2)
            style = 'List Bullet 2' if indent >= 2 else 'List Bullet'
            p = doc.add_paragraph(style=style)
            bold_color = MRN if content.startswith('**') else None
            add_runs(p, content, bold_color=bold_color); continue
        p = doc.add_paragraph(); add_runs(p, s)

    doc.save(DOCX)
    print(f"DOCX -> {DOCX}")


if __name__ == "__main__":
    build_pdf()
    build_docx()
