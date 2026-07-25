#!/usr/bin/env python3
"""Build docs/Packet.pdf and docs/Packet.docx from docs/Packet.md.

The Packet mirrors the original lab-packet visual design, rebranded for
CordInsight: centered logo, maroon (#6C1D45) headings with light-gray
underlines, gold (#EAAA00) section rule, slate (#333F48) sub-headings,
bold-maroon "Prompt N:" / "Module N:" labels, and evenly-spaced answer lines.

Fonts are sourced from macOS system fonts (Arial Unicode for full glyph
coverage incl. ☐ → ₀ ₁, Arial Bold for bold weight). Requires:
    pip install markdown xhtml2pdf python-docx
Usage:
    python3 scripts/build_packet.py
"""
import re, shutil, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MD   = ROOT / "docs" / "Packet.md"
PDF  = ROOT / "docs" / "Packet.pdf"
DOCX = ROOT / "docs" / "Packet.docx"
LOGO = ROOT / "assets" / "Logo-CI-WhiteBG.jpeg"

MAROON="#6C1D45"; GOLD="#EAAA00"; SLATE="#333F48"; GRAY="#3a3a3a"

# --- fonts: copy system faces to space-free temp paths for xhtml2pdf ---
_FONT_SRC = {
    "regular": "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "bold":    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "italic":     "/System/Library/Fonts/Supplemental/Arial Italic.ttf",
    "bolditalic": "/System/Library/Fonts/Supplemental/Arial Bold Italic.ttf",
}
_tmp = Path(tempfile.gettempdir())
FONT_REG = _tmp / "cordinsight_pkt_regular.ttf"
FONT_BLD = _tmp / "cordinsight_pkt_bold.ttf"
FONT_ITAL = _tmp / "cordinsight_pkt_italic.ttf"
FONT_BLDITAL = _tmp / "cordinsight_pkt_bolditalic.ttf"
for dst, key in ((FONT_REG, "regular"), (FONT_BLD, "bold"),
                 (FONT_ITAL, "italic"), (FONT_BLDITAL, "bolditalic")):
    src = Path(_FONT_SRC[key])
    if src.exists() and not dst.exists():
        shutil.copy(src, dst)


def build_pdf():
    import markdown
    from xhtml2pdf import pisa

    s = MD.read_text()
    s = s.replace('src="../assets/', f'src="{ROOT}/assets/')
    # answer-blank underscore lines -> distinct answer-line divs
    s = re.sub(r'^_{20,}\s*$', '<div class="ans">&nbsp;</div>', s, flags=re.M)
    html = markdown.markdown(s, extensions=['tables', 'sane_lists'])
    # section separators: xhtml2pdf draws <hr> black -> gold-filled div
    html = re.sub(r'<hr\s*/?>', '<div class="rule">&nbsp;</div>', html)
    # "Prompt N:" label bold-maroon
    html = re.sub(r'<strong>(Prompt \d+:)</strong>', r'<span class="lbl">\1</span>', html)
    # TOC "Module N:"/"Appendix X:" label bold-maroon, no bullet
    html = re.sub(r'<li>\s*(?:<p>)?(Module \d+:|Appendix [A-Z]:)',
                  r'<li class="toc"><span class="lbl">\1</span>', html)
    # bold list lead-ins (parents of nested sub-bullets) -> maroon-bold
    html = re.sub(r'(<li>\s*(?:<p>)?)<strong>(.*?)</strong>', r'\1<span class="lbl">\2</span>', html)
    # H0/H1 labels carry subscript glyphs Arial Bold lacks -> keep regular weight
    html = html.replace('class="lbl">Null Hypothesis', 'class="lblr">Null Hypothesis')
    html = html.replace('class="lbl">Alternative Hypothesis', 'class="lblr">Alternative Hypothesis')
    # checkbox options: drop the list bullet, keep just the ☐ marker
    html = re.sub(r'<li>(\s*(?:<p>)?☐)', r'<li class="cb">\1', html)

    css = f"""<style>
@font-face {{ font-family: S; src: url('{FONT_REG}'); }}
@font-face {{ font-family: S; src: url('{FONT_BLD}'); font-weight: bold; }}
@font-face {{ font-family: S; src: url('{FONT_ITAL}'); font-style: italic; }}
@font-face {{ font-family: S; src: url('{FONT_BLDITAL}'); font-weight: bold; font-style: italic; }}
@page {{ size: letter; margin: 2cm 2cm 2.2cm 2cm;
  @frame footer {{ -pdf-frame-content: footer; bottom: 1cm; height: 1cm; }} }}
body {{ font-family: S; font-size: 11pt; line-height: 1.4; color: {GRAY}; }}
h1 {{ color: {MAROON}; font-size: 25pt; text-align: center; margin: 6px 0 2px; }}
.subtitle {{ text-align:center; color:{SLATE}; font-size: 13pt; margin: 0 0 4px; }}
h2 {{ color: {MAROON}; font-size: 17pt; margin: 18px 0 8px; padding-bottom: 4px;
     border-bottom: 1px solid #cfcfcf; }}
h3 {{ color: {SLATE}; font-size: 12.5pt; margin: 14px 0 4px; }}
h4 {{ color: {SLATE}; font-size: 11.5pt; margin: 10px 0 3px; }}
p {{ margin: 5px 0; }}
ul,ol {{ margin: 3px 0 8px 18px; }} li {{ margin: 2px 0; }}
ul {{ list-style-type: disc; }}
ul ul {{ list-style-type: circle; margin: 2px 0 4px 16px; }}
li.cb {{ list-style-type: none; }}
li.toc {{ list-style-type: none; margin: 3px 0; }}
.lbl {{ color: {MAROON}; font-weight: bold; }}
.lblr {{ color: {MAROON}; font-weight: normal; }}
.rule {{ background-color: {GOLD}; height: 2px; line-height: 2px; font-size: 2px; margin: 14px 0; }}
.ans {{ border-bottom: 1px solid #bbbbbb; padding-top: 15px; margin-bottom: 7px; font-size: 1px; line-height: 1px; }}
table {{ border-collapse: collapse; margin: 8px 0; }}
th,td {{ border: 1px solid #bbb; padding: 4px 7px; font-size: 10pt; }}
th {{ background: #f3e7cd; color: {SLATE}; }}
img {{ width: 250px; }}
</style>"""
    foot = '<div id="footer" style="text-align:right; color:#888; font-size:9pt;"><pdf:pagenumber></div>'
    out = f"<html><head>{css}</head><body>{html}{foot}</body></html>"
    with open(PDF, "wb") as f:
        err = pisa.CreatePDF(out, dest=f, encoding='utf-8').err
    print(f"PDF  -> {PDF}  (err={err})")


def build_docx():
    from docx import Document
    from docx.shared import Pt, Inches, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement

    MRN=RGBColor(0x6C,0x1D,0x45); SLT=RGBColor(0x33,0x3F,0x48)
    GRY=RGBColor(0x3A,0x3A,0x3A); GLD="EAAA00"; LGRAY="CFCFCF"; LINEGRAY="BBBBBB"
    FONT="Segoe UI"

    doc=Document()
    st=doc.styles['Normal']; st.font.name=FONT; st.font.size=Pt(11); st.font.color.rgb=GRY
    doc.styles['Normal'].paragraph_format.space_after=Pt(6)
    for m in doc.sections:
        m.top_margin=m.bottom_margin=Inches(0.9); m.left_margin=m.right_margin=Inches(0.9)

    def bottom_border(p, color, sz):
        pPr=p._p.get_or_add_pPr(); pbdr=OxmlElement('w:pBdr'); b=OxmlElement('w:bottom')
        b.set(qn('w:val'),'single'); b.set(qn('w:sz'),str(sz)); b.set(qn('w:space'),'2'); b.set(qn('w:color'),color)
        pbdr.append(b); pPr.append(pbdr)

    def add_runs(p, text, bold_color=None, base_color=GRY):
        # Normalize inline HTML to markdown so <em>/<strong> render as italic/bold
        # in the DOCX (the PDF path goes through markdown and handles them already).
        text = re.sub(r'</?(strong|b)>', '**', text)
        text = re.sub(r'</?(em|i)>', '*', text)
        for seg in re.split(r'(\*\*.+?\*\*|\*.+?\*)', text):
            if not seg: continue
            if seg.startswith('**') and seg.endswith('**'):
                r=p.add_run(seg[2:-2]); r.bold=True; r.font.color.rgb=bold_color or base_color
            elif seg.startswith('*') and seg.endswith('*') and len(seg)>2:
                r=p.add_run(seg[1:-1]); r.italic=True; r.font.color.rgb=base_color
            else:
                r=p.add_run(seg); r.font.color.rgb=base_color
            r.font.name=FONT

    def heading(text, size, color, rule=False):
        p=doc.add_paragraph(); p.paragraph_format.space_before=Pt(12); p.paragraph_format.space_after=Pt(4)
        r=p.add_run(text); r.bold=True; r.font.size=Pt(size); r.font.color.rgb=color; r.font.name=FONT
        if rule: bottom_border(p, LGRAY, 6)

    lines=MD.read_text().split("\n"); i=0
    while i<len(lines):
        raw=lines[i]; s=raw.strip(); i+=1
        if s=="": continue
        if '<img' in s:
            p=doc.add_paragraph(); p.alignment=WD_ALIGN_PARAGRAPH.CENTER
            p.add_run().add_picture(str(LOGO), width=Inches(2.6)); continue
        if s in ('<p align="center">','</p>'): continue
        if s.startswith('<p align="center" class="subtitle"'):
            p=doc.add_paragraph(); p.alignment=WD_ALIGN_PARAGRAPH.CENTER; p.paragraph_format.space_after=Pt(4)
            for txt,ital in [("Zebrafish (",False),("Danio rerio",True),(") Behavioral Research",False)]:
                r=p.add_run(txt); r.bold=True; r.italic=ital; r.font.size=Pt(13); r.font.color.rgb=SLT; r.font.name=FONT
            continue
        if s=='---':
            p=doc.add_paragraph(); p.paragraph_format.space_before=Pt(2); p.paragraph_format.space_after=Pt(8)
            bottom_border(p, GLD, 18); continue
        if s.startswith('# '):
            p=doc.add_paragraph(); p.alignment=WD_ALIGN_PARAGRAPH.CENTER; p.paragraph_format.space_after=Pt(2)
            r=p.add_run(s[2:]); r.bold=True; r.font.size=Pt(24); r.font.color.rgb=MRN; r.font.name=FONT; continue
        if s.startswith('## '): heading(s[3:], 16, MRN, rule=True); continue
        if s.startswith('### '): heading(s[4:], 13, SLT); continue
        if re.match(r'^\d+\. ', s):
            p=doc.add_paragraph(style='List Number'); add_runs(p, re.sub(r'^\d+\. ','',s)); continue
        if re.match(r'^_{20,}$', s):
            p=doc.add_paragraph(); p.paragraph_format.space_before=Pt(14); p.paragraph_format.space_after=Pt(3)
            bottom_border(p, LINEGRAY, 6); continue
        mb=re.match(r'^(\s*)- (.*)$', raw)
        if mb:
            indent=len(mb.group(1)); content=mb.group(2); lvl=1 if indent>=2 else 0
            if content.startswith('☐'):
                p=doc.add_paragraph(); p.paragraph_format.left_indent=Inches(0.5); p.paragraph_format.space_after=Pt(3)
                add_runs(p, content); continue
            mtoc=re.match(r'^(Module \d+:|Appendix [A-Z]:)\s*(.*)$', content)
            if mtoc:
                p=doc.add_paragraph(); p.paragraph_format.left_indent=Inches(0.4); p.paragraph_format.space_after=Pt(3)
                r=p.add_run(mtoc.group(1)); r.bold=True; r.font.color.rgb=MRN; r.font.name=FONT
                add_runs(p, ' '+mtoc.group(2)); continue
            style='List Bullet 2' if lvl==1 else 'List Bullet'
            p=doc.add_paragraph(style=style)
            bold_color=MRN if content.startswith('**') else None
            add_runs(p, content, bold_color=bold_color); continue
        bold_color=MRN if s.startswith('**Prompt') else None
        p=doc.add_paragraph(); add_runs(p, s, bold_color=bold_color)

    doc.save(DOCX)
    print(f"DOCX -> {DOCX}")


if __name__ == "__main__":
    build_pdf()
    build_docx()
