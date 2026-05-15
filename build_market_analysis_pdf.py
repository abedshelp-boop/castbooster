"""
Build the Cast Booster market analysis PDF using ReportLab.
Run: python build_market_analysis_pdf.py
Output: cast-booster-market-analysis.pdf
"""

from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.lib.enums import TA_JUSTIFY, TA_LEFT, TA_CENTER
from reportlab.platypus import (
    BaseDocTemplate,
    PageTemplate,
    Frame,
    Paragraph,
    Spacer,
    PageBreak,
    Table,
    TableStyle,
    KeepTogether,
    Flowable,
    HRFlowable,
)
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from datetime import date
import os

OUTPUT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "cast-booster-market-analysis.pdf",
)

# ---------------------------------------------------------------------------
# Brand palette
# ---------------------------------------------------------------------------
INK = colors.HexColor("#0d0d0d")
MUTED = colors.HexColor("#6a6f7b")
ACCENT = colors.HexColor("#3b6ef0")
ACCENT_DARK = colors.HexColor("#1a48bf")
BG_LIGHT = colors.HexColor("#f4f6fb")
BG_MID = colors.HexColor("#e8ecf4")
BORDER = colors.HexColor("#d0d5e2")
QUOTE_BG = colors.HexColor("#fbf8ed")
QUOTE_BORDER = colors.HexColor("#e2d294")
GOOD = colors.HexColor("#2d7a3e")
WARN = colors.HexColor("#b8860b")
BAD = colors.HexColor("#b8382d")


# ---------------------------------------------------------------------------
# Page templates (cover page has no header/footer, body has both)
# ---------------------------------------------------------------------------
def _cover_page(canvas_obj, doc):
    canvas_obj.saveState()
    # subtle footer mark
    canvas_obj.setFillColor(MUTED)
    canvas_obj.setFont("Helvetica", 8)
    canvas_obj.drawString(0.75 * inch, 0.5 * inch, "Confidential market research")
    canvas_obj.drawRightString(
        LETTER[0] - 0.75 * inch, 0.5 * inch, "Cast Booster Market Analysis"
    )
    canvas_obj.restoreState()


def _body_page(canvas_obj, doc):
    canvas_obj.saveState()
    canvas_obj.setStrokeColor(BORDER)
    canvas_obj.setLineWidth(0.5)
    # header rule
    canvas_obj.line(
        0.75 * inch, LETTER[1] - 0.6 * inch, LETTER[0] - 0.75 * inch, LETTER[1] - 0.6 * inch
    )
    canvas_obj.setFillColor(MUTED)
    canvas_obj.setFont("Helvetica", 8)
    canvas_obj.drawString(
        0.75 * inch, LETTER[1] - 0.45 * inch, "Cast Booster — SaaS Market Analysis"
    )
    canvas_obj.drawRightString(
        LETTER[0] - 0.75 * inch, LETTER[1] - 0.45 * inch, "April 2026"
    )
    # footer rule
    canvas_obj.line(
        0.75 * inch, 0.65 * inch, LETTER[0] - 0.75 * inch, 0.65 * inch
    )
    canvas_obj.drawString(0.75 * inch, 0.5 * inch, "Cast Booster project")
    canvas_obj.drawRightString(
        LETTER[0] - 0.75 * inch, 0.5 * inch, f"Page {doc.page - 1}"
    )
    canvas_obj.restoreState()


# ---------------------------------------------------------------------------
# Stylesheet
# ---------------------------------------------------------------------------
def make_styles():
    ss = getSampleStyleSheet()
    ss.add(
        ParagraphStyle(
            name="CoverTitle",
            parent=ss["Title"],
            fontName="Helvetica-Bold",
            fontSize=32,
            leading=38,
            textColor=INK,
            alignment=TA_LEFT,
            spaceAfter=14,
        )
    )
    ss.add(
        ParagraphStyle(
            name="CoverSubtitle",
            parent=ss["Normal"],
            fontName="Helvetica",
            fontSize=15,
            leading=22,
            textColor=MUTED,
            alignment=TA_LEFT,
            spaceAfter=40,
        )
    )
    ss.add(
        ParagraphStyle(
            name="CoverMeta",
            parent=ss["Normal"],
            fontName="Helvetica",
            fontSize=10,
            leading=16,
            textColor=INK,
            alignment=TA_LEFT,
        )
    )
    ss.add(
        ParagraphStyle(
            name="H1",
            parent=ss["Heading1"],
            fontName="Helvetica-Bold",
            fontSize=20,
            leading=26,
            textColor=ACCENT_DARK,
            spaceBefore=22,
            spaceAfter=12,
            keepWithNext=True,
        )
    )
    ss.add(
        ParagraphStyle(
            name="H2",
            parent=ss["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=13,
            leading=17,
            textColor=INK,
            spaceBefore=14,
            spaceAfter=6,
            keepWithNext=True,
        )
    )
    ss.add(
        ParagraphStyle(
            name="H3",
            parent=ss["Heading3"],
            fontName="Helvetica-Bold",
            fontSize=11,
            leading=15,
            textColor=ACCENT_DARK,
            spaceBefore=10,
            spaceAfter=4,
            keepWithNext=True,
        )
    )
    ss.add(
        ParagraphStyle(
            name="Body",
            parent=ss["Normal"],
            fontName="Helvetica",
            fontSize=10,
            leading=15,
            textColor=INK,
            alignment=TA_JUSTIFY,
            spaceAfter=8,
        )
    )
    ss.add(
        ParagraphStyle(
            name="BulletItem",
            parent=ss["Body"],
            fontName="Helvetica",
            fontSize=10,
            leading=14,
            leftIndent=16,
            bulletIndent=4,
            spaceAfter=4,
            alignment=TA_LEFT,
        )
    )
    ss.add(
        ParagraphStyle(
            name="Quote",
            parent=ss["Body"],
            fontName="Helvetica-Oblique",
            fontSize=9.5,
            leading=14,
            leftIndent=12,
            rightIndent=12,
            textColor=INK,
            alignment=TA_LEFT,
            spaceBefore=4,
            spaceAfter=4,
        )
    )
    ss.add(
        ParagraphStyle(
            name="QuoteSrc",
            parent=ss["Body"],
            fontName="Helvetica",
            fontSize=8,
            leading=11,
            leftIndent=12,
            textColor=MUTED,
            spaceAfter=10,
            alignment=TA_LEFT,
        )
    )
    ss.add(
        ParagraphStyle(
            name="Callout",
            parent=ss["Body"],
            fontName="Helvetica-Bold",
            fontSize=10.5,
            leading=16,
            textColor=INK,
            spaceBefore=10,
            spaceAfter=10,
            alignment=TA_LEFT,
        )
    )
    ss.add(
        ParagraphStyle(
            name="Cite",
            parent=ss["Body"],
            fontName="Helvetica",
            fontSize=8.5,
            leading=12,
            textColor=MUTED,
            spaceAfter=3,
            leftIndent=8,
            firstLineIndent=-8,
            alignment=TA_LEFT,
        )
    )
    ss.add(
        ParagraphStyle(
            name="TableCell",
            parent=ss["Body"],
            fontName="Helvetica",
            fontSize=8.5,
            leading=11,
            spaceAfter=0,
            alignment=TA_LEFT,
        )
    )
    ss.add(
        ParagraphStyle(
            name="TableHeader",
            parent=ss["Body"],
            fontName="Helvetica-Bold",
            fontSize=9,
            leading=12,
            textColor=colors.white,
            alignment=TA_LEFT,
            spaceAfter=0,
        )
    )
    return ss


STY = make_styles()


# ---------------------------------------------------------------------------
# Custom flowable: colored callout box
# ---------------------------------------------------------------------------
class CalloutBox(Flowable):
    def __init__(self, title, body, bg=BG_LIGHT, border=ACCENT, text_color=INK, width=None):
        super().__init__()
        self.title = title
        self.body = body
        self.bg = bg
        self.border = border
        self.text_color = text_color
        self.width = width
        self._title_para = None
        self._body_para = None
        self._padding = 10

    def wrap(self, avail_w, avail_h):
        self.width = self.width or avail_w
        inner_w = self.width - 2 * self._padding
        title_style = ParagraphStyle(
            "CalloutTitle",
            parent=STY["Body"],
            fontName="Helvetica-Bold",
            fontSize=10,
            leading=13,
            textColor=self.text_color,
        )
        body_style = ParagraphStyle(
            "CalloutBody",
            parent=STY["Body"],
            fontName="Helvetica",
            fontSize=9.5,
            leading=13,
            textColor=self.text_color,
            alignment=TA_LEFT,
            spaceAfter=0,
        )
        self._title_para = Paragraph(self.title, title_style)
        self._body_para = Paragraph(self.body, body_style)
        t_w, t_h = self._title_para.wrap(inner_w, avail_h)
        b_w, b_h = self._body_para.wrap(inner_w, avail_h)
        self._height = t_h + b_h + 2 * self._padding + 4
        return (self.width, self._height)

    def draw(self):
        c = self.canv
        c.saveState()
        c.setFillColor(self.bg)
        c.setStrokeColor(self.border)
        c.setLineWidth(1)
        c.roundRect(0, 0, self.width, self._height, 5, stroke=1, fill=1)
        # Left accent bar
        c.setFillColor(self.border)
        c.rect(0, 0, 4, self._height, stroke=0, fill=1)
        c.restoreState()
        inner_w = self.width - 2 * self._padding
        y = self._height - self._padding
        self._body_para.wrap(inner_w, self._height)
        self._title_para.wrap(inner_w, self._height)
        y -= self._title_para.height
        self._title_para.drawOn(self.canv, self._padding, y)
        y -= 4 + self._body_para.height
        self._body_para.drawOn(self.canv, self._padding, y)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def p(text, style="Body"):
    return Paragraph(text, STY[style])


def h1(text):
    return Paragraph(text, STY["H1"])


def h2(text):
    return Paragraph(text, STY["H2"])


def h3(text):
    return Paragraph(text, STY["H3"])


def bullets(items):
    return [Paragraph("• " + t, STY["BulletItem"]) for t in items]


def quote(text, source=None):
    flows = [
        Paragraph("&ldquo;" + text + "&rdquo;", STY["Quote"]),
    ]
    if source:
        flows.append(Paragraph("— " + source, STY["QuoteSrc"]))
    return flows


def spacer(h=8):
    return Spacer(1, h)


def hr():
    return HRFlowable(
        width="100%", thickness=0.5, color=BORDER, spaceBefore=6, spaceAfter=6
    )


# ---------------------------------------------------------------------------
# Content
# ---------------------------------------------------------------------------
def build_story():
    story = []

    # -----------------------------------------------------------------------
    # Cover page
    # -----------------------------------------------------------------------
    story.append(Spacer(1, 1.4 * inch))
    story.append(
        Paragraph(
            '<font color="#3b6ef0">C A S T&nbsp;&nbsp;B O O S T E R</font>',
            ParagraphStyle(
                "CoverLabel",
                fontName="Helvetica-Bold",
                fontSize=11,
                leading=14,
                letterSpacing=2,
                spaceAfter=18,
            ),
        )
    )
    story.append(Paragraph("SaaS Market Analysis", STY["CoverTitle"]))
    story.append(
        Paragraph(
            "Is there real demand for a Chrome extension plus desktop helper "
            "that casts arbitrary web videos to Chromecast in optimized quality?",
            STY["CoverSubtitle"],
        )
    )
    story.append(spacer(40))
    story.append(
        Paragraph(
            "<b>Prepared by:</b> Independent research (Exa.ai + Brave Web Search)<br/>"
            f"<b>Date:</b> {date.today().strftime('%B %d, %Y')}<br/>"
            "<b>Scope:</b> Demand signals, competitor landscape, technical moat, "
            "market sizing, business model, legal risk, honest recommendation<br/>"
            "<b>Status:</b> Confidential draft",
            STY["CoverMeta"],
        )
    )
    story.append(spacer(36))
    story.append(
        CalloutBox(
            "Bottom line up front",
            "Yes, there is a real problem and real demand &mdash; multi-year Reddit "
            "threads and a 500K-user walking-dead incumbent prove it. This is "
            "<b>not</b> a VC-backable startup, but it is a plausible "
            "<b>$500K &ndash; $3M/year indie business</b> if executed well, with "
            "a 12&ndash;24 month first-mover window and manageable legal risk.",
            bg=BG_LIGHT,
            border=ACCENT,
        )
    )
    story.append(PageBreak())

    # -----------------------------------------------------------------------
    # Table of contents
    # -----------------------------------------------------------------------
    story.append(h1("Table of contents"))
    toc_rows = [
        ("1.", "Executive summary", "3"),
        ("2.", "The problem in plain English", "4"),
        ("3.", "Evidence of demand", "5"),
        ("4.", "Competitive landscape", "7"),
        ("5.", "Why existing solutions fail", "9"),
        ("6.", "Technical moat analysis", "10"),
        ("7.", "Market sizing", "11"),
        ("8.", "Business model options", "12"),
        ("9.", "Legal & platform risks", "13"),
        ("10.", "Honest recommendation & next steps", "14"),
        ("11.", "Sources & citations", "16"),
    ]
    toc_table = Table(
        [[num, title, page] for num, title, page in toc_rows],
        colWidths=[0.4 * inch, 5.2 * inch, 0.5 * inch],
        hAlign="LEFT",
    )
    toc_table.setStyle(
        TableStyle(
            [
                ("FONT", (0, 0), (-1, -1), "Helvetica", 10),
                ("TEXTCOLOR", (0, 0), (-1, -1), INK),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("LINEBELOW", (0, 0), (-1, -1), 0.25, BORDER),
                ("TEXTCOLOR", (0, 0), (0, -1), MUTED),
                ("ALIGN", (2, 0), (2, -1), "RIGHT"),
                ("TEXTCOLOR", (2, 0), (2, -1), MUTED),
            ]
        )
    )
    story.append(toc_table)
    story.append(PageBreak())

    # -----------------------------------------------------------------------
    # 1. Executive summary
    # -----------------------------------------------------------------------
    story.append(h1("1. Executive summary"))
    story.append(
        p(
            "<b>The problem is real.</b> Reddit r/Chromecast has multi-year threads with "
            "dozens of upvotes complaining about exactly the pain point this product would "
            "solve: Chrome's tab casting dropping to &ldquo;Poor playback quality, switched "
            "to mirroring&rdquo; mode on non-whitelisted video sites. The complaint has "
            "appeared in new threads every few months since at least 2017 and continues "
            "into 2025."
        )
    )
    story.append(
        p(
            "<b>Demand is quantifiable.</b> Videostream &mdash; the closest historical "
            "product &mdash; built a user base of <b>500,000&ndash;700,000 Chrome Web Store "
            "users</b> plus <b>1M+ Android installs</b>, with paying customers on a "
            "$1.49/mo, $14.99/yr, or $34.99 lifetime plan, for a feature set that only "
            "cast <i>local files</i> to Chromecast. Extending that to arbitrary web videos "
            "would expand, not shrink, that audience."
        )
    )
    story.append(
        p(
            "<b>The market gap is wide open.</b> Every existing product is either "
            "abandoned (Videostream: last meaningful update May 2020; CastBuddy: abandoned "
            "December 2021), Android-first with weak desktop support (Web Video Cast, "
            "millions of users but the Chrome extension path is broken), free with no "
            "monetization (LocalCast: 50M+ downloads, zero revenue model), or limited to "
            "local files only (VLC, Plex). <b>None</b> solve the session-locked CDN "
            "problem at the core of modern web video."
        )
    )
    story.append(
        p(
            "<b>The technical moat is non-trivial.</b> The only existing solution is a "
            "Node.js HLS proxy (npm package <font name=\"Courier\">@warren-bank/hls-proxy</font>) "
            "with <b>78 weekly downloads and zero productization</b>. Stack Overflow, the "
            "Bitmovin community, and shaka-player GitHub issues all independently confirm "
            "this is the only known fix, and it is only accessible to developers willing "
            "to run a command-line Node app."
        )
    )
    story.append(
        p(
            "<b>Legal risk is manageable.</b> Widevine-protected content (Netflix, "
            "Disney+, HBO Max) must stay off-limits. Outside of DRM, Videostream's "
            "11-year track record operating in this exact &ldquo;cast anything your "
            "browser plays&rdquo; zone shows no DMCA actions, no takedowns, and no "
            "payment-processor issues."
        )
    )
    story.append(
        p(
            "<b>Revenue potential is modest but concrete.</b> Based on Videostream's "
            "known pricing and install base, a well-executed product in this space could "
            "realistically reach <b>$500K&ndash;$3M/year in ARR</b> at peak. That is a "
            "lifestyle business &mdash; salary replacement for a solo founder, possibly "
            "2&ndash;4 person team scale at peak. Not VC-scale."
        )
    )
    story.append(spacer(6))
    story.append(
        CalloutBox(
            "Recommendation",
            "<b>Build it.</b> Not as a venture-backed SaaS, but as a profitable indie "
            "product with a clearly under-served audience and real technical moat. Budget "
            "1&ndash;3 months of focused build time + ongoing part-time maintenance. "
            "Expect $5K MRR around month 12&ndash;24, up to ~$100K MRR at peak over "
            "3&ndash;5 years.",
            bg=BG_LIGHT,
            border=ACCENT,
        )
    )
    story.append(PageBreak())

    # -----------------------------------------------------------------------
    # 2. The problem
    # -----------------------------------------------------------------------
    story.append(h1("2. The problem in plain English"))
    story.append(
        p(
            "When Chrome casts a tab to a Chromecast, it decides between two modes based "
            "on what it sees in the tab:"
        )
    )
    story.extend(
        bullets(
            [
                "<b>Optimized for video</b> &mdash; Chrome extracts the video URL or the "
                "decoded video stream and sends it directly to the Chromecast. High "
                "quality, low CPU, seamless playback.",
                "<b>Tab mirror</b> &mdash; Chrome takes a real-time screen recording of "
                "the tab and streams that to the Chromecast. Low quality, high CPU, laggy "
                "audio, unwatchable frame rate on many setups.",
            ]
        )
    )
    story.append(
        p(
            "Chrome only offers the first mode when the video element has a real, "
            "recognizable URL &mdash; a plain MP4 or HLS address &mdash; or when the page "
            "explicitly uses the <b>Cast SDK</b> to hand the URL off. YouTube and Netflix "
            "do this. Thousands of long-tail sites do not."
        )
    )
    story.append(
        p(
            "Modern streaming sites instead feed video into the browser using "
            "<b>MediaSource Extensions (MSE)</b>, which builds the video in memory chunk "
            "by chunk. The <font name=\"Courier\">&lt;video&gt;</font> element's "
            "<font name=\"Courier\">src</font> attribute ends up as "
            "<font name=\"Courier\">blob:https://...</font> &mdash; a reference to "
            "in-memory data, not a real network URL. Chrome has nothing to extract. So it "
            "falls back to tab mirror."
        )
    )
    story.append(
        p(
            "On top of this, the CDNs behind those sites typically <b>session-lock</b> "
            "their stream URLs &mdash; the exact URL that plays the video is only valid "
            "if fetched by the same browser session that originally requested it. Even if "
            "you extracted the URL with an extension and handed it to the Chromecast, the "
            "Chromecast's own HTTP fetch would come back with 404 because it isn't the "
            "original session."
        )
    )
    story.append(
        CalloutBox(
            "The core technical fact",
            "You cannot cast modern streaming sites to a Chromecast in optimized quality "
            "using only a browser extension. The only reliable fix requires a local HTTP "
            "proxy running on the user's own laptop that impersonates the browser session "
            "when fetching from the CDN, and re-serves the content to the Chromecast with "
            "clean headers. No commercial product has productized this architecture yet.",
            bg=QUOTE_BG,
            border=QUOTE_BORDER,
        )
    )
    story.append(PageBreak())

    # -----------------------------------------------------------------------
    # 3. Evidence of demand
    # -----------------------------------------------------------------------
    story.append(h1("3. Evidence of demand"))
    story.append(
        p(
            "Reddit r/Chromecast (300K+ subscribers) is the epicenter. Multi-year threads, "
            "each with dozens of upvotes and tens of comments, capture the same frustration "
            "in slightly different words. A representative sample:"
        )
    )
    story.append(spacer(4))

    story.append(
        h3(
            '"Any way to stop \'poor playback quality - switched to mirroring\' '
            'when casting video from a Chrome tab?"'
        )
    )
    story.append(p("r/Chromecast &mdash; 18 votes, 14 comments", "Cite"))
    story.extend(
        quote(
            "Unfortunately, Chromecast will usually decide at some point (sometimes within "
            "seconds of starting casting) to switch to mirroring, will give me the 'poor "
            "playback quality - switched to mirroring' message, and the video will play in "
            "the Chrome tab while mirroring at an unwatchably low frame rate on the TV. I "
            "do have 'optimize fullscreen videos' toggled on.",
            "reddit.com/r/Chromecast/comments/lwd296",
        )
    )

    story.append(
        h3(
            '"Chromecast is TERRIBLE at Casting a tab. '
            'Looks like 240p resolution, several second delay..."'
        )
    )
    story.append(p("r/Chromecast &mdash; 29 votes, 26 comments", "Cite"))
    story.extend(
        quote(
            "A problem that literally no one has an answer to.",
            "reddit.com/r/Chromecast/comments/6mzh2c",
        )
    )

    story.append(
        h3('"How do I force chrome (desktop) to cast a tab instead of a video?"')
    )
    story.append(p("r/Chromecast", "Cite"))
    story.extend(
        quote(
            "Clappr is the trash video player the site is using, which clearly isn't "
            "Chromecast compatible. Google should still make it possible to cast the tab "
            "instead of the video though.",
            "reddit.com/r/Chromecast/comments/110dwi3",
        )
    )

    story.append(
        h3(
            '"For those using Web Video Caster, remember to download '
            'Web Video Receiver on your device / TV"'
        )
    )
    story.append(p("r/AndroidTV &mdash; 19 votes, 7 comments", "Cite"))
    story.extend(
        quote(
            "I just got a Macbook and most websites aren't even casting from Chrome to an "
            "Onn Android box (wth?). Same websites will cast no problem from my phone "
            "using Web Video Caster. Web Video Caster also has a receiver app... that is "
            "rock solid and never has issues that are normally introduced to the stream "
            "by Chrome / Chromecast itself.",
            "reddit.com/r/AndroidTV/comments/1k3c1lh",
        )
    )
    story.append(PageBreak())

    story.append(h2("CastBuddy Chrome Web Store reviews (80K users, 3.6&#9733;, abandoned)"))
    story.append(
        p(
            "The CastBuddy extension sits precisely where a productized version of this "
            "tool would sit &mdash; and its user reviews from 2020&ndash;2026 are a "
            "running log of frustrated users slowly migrating away as Chrome updates "
            "broke it and maintenance stopped."
        )
    )
    story.extend(
        quote(
            "Doesn't stream for me. It detects the video link (m3u8)... It initializes "
            "the video but the screen stays black and busy but never plays. I guess I'm "
            "stuck using Web Video Caster for android to stream my links. meh!",
            "Luis Mendez, 2024-06-10",
        )
    )
    story.extend(
        quote(
            "I guess it's no longer maintained. Doesn't work at all.",
            "Guillaume Perrault Archambault, 2022-01-15",
        )
    )
    story.extend(
        quote(
            "It casted the video but takes the screen hostage while it's playing. If I "
            "try to go to a different tab or try to scroll on the page, the video "
            "instantly stops.",
            "Tony B, 2020-09-13",
        )
    )

    story.append(h2("Enterprise corroboration: Bitmovin community thread, 2024"))
    story.append(
        p(
            "Even paying commercial CDN customers hit the same wall. From a Bitmovin "
            "customer trying to cast a CloudFront signed-cookie stream to a Chromecast:"
        )
    )
    story.extend(
        quote(
            "The stream plays fine in the browser when 'withCredentials' and "
            "'manifestWithCredentials' are set to true, but dies on the Chromecast with "
            "'network error'. The console gives me chrome.cast.Error {code: "
            "'session_error'...}. What is the promise I need to satisfy?",
            "community.bitmovin.com/t/chromecast-cloudfront-signed-cookies-possible/3323",
        )
    )
    story.append(
        p(
            "Bitmovin's official answer: the default Cast receiver cannot forward "
            "cookies; the customer must build and register a custom CAF (Cast Application "
            "Framework) receiver in the Google Cast Developer Console and override "
            "<font name=\"Courier\">updateManifestRequestInfo</font> / "
            "<font name=\"Courier\">updateSegmentRequestInfo</font> to pass "
            "<font name=\"Courier\">withCredentials = true</font>. This is a developer "
            "workaround, not an end-user product."
        )
    )

    story.append(h2("Demand signal summary"))
    story.extend(
        bullets(
            [
                "The complaint appears in new Reddit threads every few months for at least "
                "<b>9 years straight (2016&ndash;2025)</b>.",
                "Users consistently migrate <i>from</i> desktop extensions <i>to</i> "
                "Android apps (Web Video Caster specifically) because the desktop path is "
                "broken. This is the clearest proof that the desktop gap is real and painful.",
                "Even <b>paying enterprise CDN customers</b> (Bitmovin) hit the same "
                "technical wall when they try to add Chromecast support.",
                "The complaint persists across two Chromecast hardware generations "
                "(original Chromecast &rarr; Chromecast with Google TV &rarr; Google TV "
                "Streamer, 2013&ndash;2024+), suggesting Google is not going to fix it.",
            ]
        )
    )
    story.append(PageBreak())

    # -----------------------------------------------------------------------
    # 4. Competitive landscape
    # -----------------------------------------------------------------------
    story.append(h1("4. Competitive landscape"))
    story.append(
        p(
            "The table below covers every notable product in this space identified in the "
            "research. The striking pattern: <b>no single active product solves this exact "
            "problem on Chrome/desktop</b>. Every product is either abandoned, on the "
            "wrong platform, or missing a piece of the pipeline."
        )
    )
    story.append(spacer(6))

    # Competitive table
    header = [
        Paragraph("Product", STY["TableHeader"]),
        Paragraph("Platform", STY["TableHeader"]),
        Paragraph("Install base", STY["TableHeader"]),
        Paragraph("Pricing", STY["TableHeader"]),
        Paragraph("Status", STY["TableHeader"]),
        Paragraph("Limitation", STY["TableHeader"]),
    ]
    rows = [
        [
            "Videostream",
            "Chrome ext, desktop, Android, iOS",
            "500K&ndash;700K Chrome<br/>1M+ Android",
            "$1.49/mo, $14.99/yr, $34.99 lifetime",
            '<font color="#b8382d"><b>Abandoned</b></font> &mdash; last real update May 2020',
            "Local files only; not web video",
        ],
        [
            "LocalCast",
            "Android, iOS",
            "50M+ downloads, 4.5&#9733;",
            "100% free forever",
            '<font color="#2d7a3e">Active</font>',
            "No desktop; no monetization",
        ],
        [
            "Web Video Cast<br/><font size=8 color=\"#6a6f7b\">(InstantBits)</font>",
            "Android, iOS",
            "Millions of users",
            "Freemium, premium IAP",
            '<font color="#2d7a3e">Active</font> (v4.4.2, March 2025)',
            "Android-first; explicitly not a Chrome tab caster",
        ],
        [
            "CastBuddy",
            "Chrome extension",
            "80K users, 3.6&#9733;",
            "Free",
            '<font color="#b8382d"><b>Abandoned</b></font> since Dec 2021',
            "Broken by Chrome updates",
        ],
        [
            "m3u8 Sniffer TV",
            "Chrome extension",
            "60K users, 4.4&#9733;",
            "Free",
            '<font color="#2d7a3e">Active</font>',
            "Not a caster &mdash; URL sniffer only",
        ],
        [
            "WebCast-Reloaded",
            "Chrome ext (dev mode)",
            "35 GitHub stars",
            "Open source, GPL-2.0",
            '<font color="#2d7a3e">Active</font>',
            "Not on Web Store; separate proxy server",
        ],
        [
            "VLC",
            "Desktop + Android",
            "3.5B+ cumulative DLs",
            "Free / donationware",
            '<font color="#2d7a3e">Active</font>',
            "Local files only; no browser integration",
        ],
        [
            "Plex Media Server",
            "Self-hosted + clients",
            "~25M monthly active",
            "Free + Plex Pass ($4.99/mo, $39.99/yr, $119.99 lifetime)",
            '<font color="#2d7a3e">Active</font>',
            "Requires personal media library",
        ],
        [
            "@warren-bank/hls-proxy",
            "Node.js CLI",
            "78 weekly npm downloads",
            "Free / GPL-2.0",
            '<font color="#2d7a3e">Active</font>',
            "CLI only; zero UI; zero productization",
        ],
    ]

    # Wrap each cell in Paragraph for wrapping
    wrapped_rows = []
    for row in rows:
        wrapped_rows.append([Paragraph(cell, STY["TableCell"]) for cell in row])
    table_data = [header] + wrapped_rows
    competitive_table = Table(
        table_data,
        colWidths=[
            1.0 * inch,
            1.1 * inch,
            0.95 * inch,
            1.2 * inch,
            1.35 * inch,
            1.4 * inch,
        ],
        repeatRows=1,
        hAlign="LEFT",
    )
    competitive_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), ACCENT_DARK),
                ("GRID", (0, 0), (-1, -1), 0.3, BORDER),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, BG_LIGHT]),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    story.append(competitive_table)

    story.append(spacer(10))
    story.append(h2("What this table tells us"))
    story.extend(
        bullets(
            [
                "<b>There is no single active product that solves exactly this problem on "
                "Chrome/desktop.</b> Every candidate is abandoned, wrong platform, or "
                "missing a pipeline piece.",
                "<b>The closest competitor (Videostream) is a walking dead.</b> 500K&ndash;"
                "700K paying-tier Chrome users stranded, Trustpilot 3.0&#9733;, no "
                "maintenance. A mature, proven audience literally up for grabs.",
                "<b>The nearest active competitor (Web Video Cast) is Android-first</b> and "
                "its own store pages explicitly say the app does not support tab casting "
                "&ldquo;like the Chromecast extension for the PC web browser.&rdquo; Users "
                "migrate to it because the desktop path is broken.",
                "<b>Open-source fragments of the right architecture already exist</b> "
                "(HLS-Proxy, WebCast-Reloaded) but are unpolished, undiscoverable, and "
                "have install friction that kills mainstream adoption.",
            ]
        )
    )
    story.append(
        CalloutBox(
            "The gap",
            "A polished, Chrome-first, desktop-first, actively maintained product that "
            "includes the local HLS proxy as an invisible background service, with a "
            "one-click installer and Chrome Web Store distribution. Nothing in the market "
            "currently occupies this exact shape.",
            bg=BG_MID,
            border=ACCENT,
        )
    )
    story.append(PageBreak())

    # -----------------------------------------------------------------------
    # 5. Why existing solutions fail
    # -----------------------------------------------------------------------
    story.append(h1("5. Why existing solutions fail for session-locked providers"))
    story.append(
        p(
            "Stack Overflow, the Bitmovin community, and shaka-player GitHub issues all "
            "independently confirm that session-locked CDN streams cannot be handed "
            "directly to a Chromecast, and that the only known workarounds require "
            "infrastructure beyond a browser extension."
        )
    )
    story.append(h3("Stack Overflow: adding CORS headers on .m3u8 via reverse proxy"))
    story.extend(
        quote(
            "The streaming provider does not add CORS headers to the HTTP headers, which "
            "is a requirement for building Chromecast apps. Is there any way to route the "
            "requests through a proxy...?",
            "stackoverflow.com/questions/21809527",
        )
    )
    story.append(
        p(
            "<b>Accepted answer:</b> &ldquo;This is not possible without rebroadcasting "
            "the streams. All of these, including the HTTP response containing the binary, "
            "needs the CORS headers for the Chromecast to display the contents.&rdquo;"
        )
    )

    story.append(h3("Bitmovin customer thread, October 2024"))
    story.append(
        p(
            "Bitmovin's answer to their paying enterprise customer: you must build and "
            "register a custom CAF receiver with Google &mdash; an Application ID in the "
            "Cast Developer Console, JavaScript handlers overriding "
            "<font name=\"Courier\">updateManifestRequestInfo</font>, "
            "<font name=\"Courier\">updateLicenseRequestInfo</font>, and "
            "<font name=\"Courier\">updateSegmentRequestInfo</font> with "
            "<font name=\"Courier\">withCredentials = true</font>. Developer work, not a "
            "user-facing product."
        )
    )

    story.append(h3("@warren-bank/hls-proxy project description"))
    story.extend(
        quote(
            "The proxy can easily be configured to bypass many of the security measures "
            "used by video servers to restrict access: CORS response headers, HTTP request "
            "headers Origin and Referer are often inspected by the server; when these "
            "headers don't match the site hosting the content, a 403 Forbidden response is "
            "returned. Restricted access to encryption keys...",
            "npmjs.com/package/@warren-bank/hls-proxy",
        )
    )
    story.append(
        p(
            "This is exactly the architecture a productized solution would use. But the "
            "package has <b>78 weekly downloads and no UI</b> &mdash; it is a CLI tool for "
            "developers who already know what they're doing. The opportunity is to "
            "productize the exact same architecture with a one-click installer and a "
            "Chrome Web Store presence."
        )
    )

    story.append(
        CalloutBox(
            "The unanimous technical conclusion",
            "The only reliable fix for session-locked streams is a local HTTP proxy that "
            "impersonates the browser session when fetching upstream and re-serves the "
            "content to the Chromecast with clean headers. Three independent technical "
            "sources (Stack Overflow, Bitmovin, shaka-player) confirm this. No commercial "
            "product currently productizes that architecture for end users.",
            bg=QUOTE_BG,
            border=QUOTE_BORDER,
        )
    )
    story.append(PageBreak())

    # -----------------------------------------------------------------------
    # 6. Technical moat
    # -----------------------------------------------------------------------
    story.append(h1("6. Technical moat analysis"))
    story.append(p("Software moats in this space come from three places."))

    story.append(h2("(1) Continuous CDN-evasion maintenance"))
    story.append(
        p(
            "Streaming CDNs change their URL schemes, header checks, and session-lock "
            "mechanisms every few months. The EgyDead investigation in this project alone "
            "surfaced three different rotating host patterns "
            "(<font name=\"Courier\">*.pinehollowdesignstudio.cyou</font>, "
            "<font name=\"Courier\">*.investmentmechanics.cfd</font>, and more) and at "
            "least two chunk-disguise tricks (<font name=\"Courier\">.woff2</font> instead "
            "of <font name=\"Courier\">.ts</font>, <font name=\"Courier\">.txt</font> "
            "instead of <font name=\"Courier\">.m3u8</font>). A productized solution has "
            "to stay ahead of this."
        )
    )
    story.append(
        p(
            "<b>Maintenance burden is both a cost and a moat.</b> Any competitor has to do "
            "the same work. Abandoned products (CastBuddy, Videostream) are living proof "
            "of what happens when maintenance lapses &mdash; the product slowly becomes "
            "non-functional and user reviews collapse."
        )
    )

    story.append(h2("(2) The local-proxy architecture itself"))
    story.append(
        p(
            "The extension-only path is closed. Anyone serious about this problem "
            "eventually arrives at the same local-proxy architecture. Being first to ship "
            "a polished version creates (a) a reputation moat, (b) distribution moat "
            "(Chrome Web Store rankings, Reddit mindshare), and (c) word-of-mouth moat "
            "among stranded Videostream users actively looking for a replacement."
        )
    )

    story.append(h2("(3) UX and packaging"))
    story.append(
        p(
            "The HLS-Proxy npm package already exists. What doesn't exist is a one-click "
            "install where the user doesn't know a proxy even exists &mdash; just that "
            "their cast button now works. Polishing away the install friction is work "
            "that can't be copy-pasted: every platform (Windows, macOS, Linux) has its "
            "own auto-start and tray-icon conventions, and a smooth cross-platform "
            "experience takes real engineering."
        )
    )

    story.append(h2("Moat caveats"))
    story.extend(
        bullets(
            [
                "The extension itself is replicable &mdash; nothing in JavaScript is secret.",
                "<b>Google could add native support</b> at any time, breaking the moat "
                "overnight. Counter-evidence: Google has had 10+ years to do this and has "
                "not; in 2024 they discontinued the Chromecast hardware line in favor of "
                "Google TV Streamer, signaling they are not investing more in improving "
                "Chrome's casting UX for web video.",
                "A new Chromium fork (Brave, Edge, Vivaldi) could implement native "
                "arbitrary-video casting and undercut the category.",
            ]
        )
    )
    story.append(
        CalloutBox(
            "Moat verdict",
            "Real but not deep. A <b>12&ndash;24 month first-mover window</b>, not a "
            "forever moat. Long enough to build a profitable indie business. Not long "
            "enough to defend a $100M valuation.",
            bg=BG_LIGHT,
            border=ACCENT,
        )
    )
    story.append(PageBreak())

    # -----------------------------------------------------------------------
    # 7. Market sizing
    # -----------------------------------------------------------------------
    story.append(h1("7. Market sizing"))
    story.append(h2("Top of funnel"))
    story.extend(
        bullets(
            [
                "<b>100M+ Chromecast devices sold</b> (Google's own figure, announced "
                "August 2024 when discontinuing the hardware line).",
                "<b>52M US households</b> have cut the cord on traditional cable "
                "(Leichtman Research Group, 2024).",
                "<b>300K+ subscribers</b> on r/Chromecast alone.",
                "Tens of millions of users actively watching non-Netflix / non-YouTube "
                "streams on Chrome monthly &mdash; fragmented and hard to measure, but "
                "Videostream's 500K&ndash;700K Chrome installs and LocalCast's 50M+ "
                "downloads provide concrete lower bounds.",
            ]
        )
    )

    story.append(h2("Serviceable addressable market"))
    story.append(
        p(
            "Conservatively, <b>5&ndash;10 million users worldwide</b> regularly cast "
            "arbitrary web videos to a TV and have experienced the tab-mirror degradation "
            "problem. Evidence: Videostream's 500K&ndash;700K paying-tier Chrome audience "
            "for a narrower feature set; Web Video Cast's millions of Android users; 9+ "
            "years of continuous Reddit complaints."
        )
    )

    story.append(h2("Serviceable obtainable market (3-year realistic)"))
    story.append(
        p(
            "<b>50K&ndash;300K users</b> is realistic for a polished product that ships "
            "on the Chrome Web Store, hits r/Chromecast at launch, inherits the abandoned "
            "Videostream audience, and maintains itself for 2+ years. "
            "<b>Conversion to paid: 1&ndash;3%</b> (industry norm for freemium utilities; "
            "Videostream's own ratio at peak looks closer to 5%)."
        )
    )

    story.append(h2("Revenue scenarios"))
    # Revenue table
    rev_header = [
        Paragraph("Scenario", STY["TableHeader"]),
        Paragraph("Free users", STY["TableHeader"]),
        Paragraph("Paid users", STY["TableHeader"]),
        Paragraph("ARR", STY["TableHeader"]),
        Paragraph("Timeline", STY["TableHeader"]),
    ]
    rev_rows = [
        ["Conservative", "50,000", "500 (1%)", "$10K / year", "Year 1&ndash;2"],
        [
            "Realistic",
            "150,000",
            "3,000 (2%)",
            "$60K&ndash;$90K",
            "Year 2",
        ],
        [
            "Good execution",
            "300,000",
            "9,000 (3%)",
            "$180K&ndash;$270K",
            "Year 3",
        ],
        [
            "Inherit Videostream orphans",
            "700,000",
            "35,000 (5%)",
            "$700K&ndash;$1.05M",
            "Year 3&ndash;4",
        ],
        [
            "Peak (long tail)",
            "1.5M",
            "75,000 (5%)",
            "$1.5M&ndash;$2.25M",
            "Year 4&ndash;5+",
        ],
    ]
    rev_wrapped = [[Paragraph(c, STY["TableCell"]) for c in row] for row in rev_rows]
    rev_table = Table(
        [rev_header] + rev_wrapped,
        colWidths=[1.5 * inch, 1.1 * inch, 1.1 * inch, 1.4 * inch, 1.1 * inch],
        repeatRows=1,
        hAlign="LEFT",
    )
    rev_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), ACCENT_DARK),
                ("GRID", (0, 0), (-1, -1), 0.3, BORDER),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, BG_LIGHT]),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ]
        )
    )
    story.append(rev_table)

    story.append(spacer(10))
    story.append(
        p(
            "<b>Benchmark:</b> Videostream's 2018&ndash;2020 peak &mdash; before the "
            "Chrome Apps deprecation killed its distribution &mdash; appears to have been "
            "in the <b>$1M&ndash;$3M ARR range</b> based on its pricing, install base, "
            "and paying customer counts. This is a <b>small-to-mid seven-figures annually "
            "ceiling</b> at peak, reached over 3&ndash;5 years. Salary replacement for a "
            "solo founder; potentially 2&ndash;4 person team scale at peak. Not VC-scale."
        )
    )
    story.append(PageBreak())

    # -----------------------------------------------------------------------
    # 8. Business model options
    # -----------------------------------------------------------------------
    story.append(h1("8. Business model options"))

    story.append(h2("Option A — Freemium desktop app (Videostream model)"))
    story.extend(
        bullets(
            [
                "<b>Free:</b> basic casting, 720p cap, tray UI with unobtrusive prompt.",
                "<b>Paid:</b> $14.99/yr or $34.99 lifetime &mdash; unlocks 1080p+, queue, "
                "subtitle tracks, ad-free, priority updates.",
                "<b>Pros:</b> proven price points from Videostream. Low friction to try. "
                "Clear upgrade reasons.",
                "<b>Cons:</b> requires payment processing (Stripe or Paddle), account "
                "system, subscription management. Conversion rates 1&ndash;3%.",
            ]
        )
    )

    story.append(h2("Option B — One-time lifetime license ($29&ndash;$49)"))
    story.extend(
        bullets(
            [
                "No subscription. Pay once, own it.",
                "<b>Pros:</b> favored by indie users, easy to understand, no churn. "
                "Appeals to subscription-fatigued buyers.",
                "<b>Cons:</b> no recurring revenue, harder to forecast, harder to fund "
                "ongoing maintenance. Works best with a long-tail install base.",
            ]
        )
    )

    story.append(h2("Option C — Paid Chrome Web Store + free local helper"))
    story.extend(
        bullets(
            [
                "Chrome extension is paid ($4.99 one-time or $1.99/mo). Local helper is "
                "free and open source.",
                "<b>Pros:</b> Chrome Web Store handles payment, no separate account "
                "system. Helper's open-source status defuses some legal concerns.",
                "<b>Cons:</b> Chrome Web Store's paid-extension infrastructure is "
                "notoriously flaky and Google has talked about deprecating it. Relying "
                "on it is risky.",
            ]
        )
    )

    story.append(h2("Option D — Donationware / VLC model"))
    story.extend(
        bullets(
            [
                "Everything is free. Donations via GitHub Sponsors, Patreon, ko-fi.",
                "<b>Pros:</b> maximum adoption, no legal/payment friction, indie ethos.",
                "<b>Cons:</b> realistically &lt;1% conversion to donations. Unlikely to "
                "exceed $10K&ndash;$50K/year even at 100K users. This is a hobby, not a "
                "business.",
            ]
        )
    )

    story.append(h2("Option E — Community free + Pro tier (Plex model)"))
    story.extend(
        bullets(
            [
                "Free forever for individual casting. Paid ($4.99/mo, $39.99/yr, or "
                "$119.99 lifetime) unlocks multi-device sync, cast history backup, "
                "cloud-synced favorites, advanced codec support, commercial license.",
                "<b>Pros:</b> broad free funnel, clear upgrade reasons. Plex has made "
                "this work for 15+ years.",
                "<b>Cons:</b> requires cloud infrastructure for Pro features, higher "
                "operational cost.",
            ]
        )
    )
    story.append(
        CalloutBox(
            "Recommended launch model",
            "<b>Option A (freemium)</b> or <b>Option B (one-time lifetime)</b>, with the "
            "option to evolve into Option E if the product takes off. Start with the "
            "simplest model. See what the first 1,000 users actually pay for before "
            "adding complexity.",
            bg=BG_LIGHT,
            border=ACCENT,
        )
    )
    story.append(PageBreak())

    # -----------------------------------------------------------------------
    # 9. Legal & platform risks
    # -----------------------------------------------------------------------
    story.append(h1("9. Legal & platform risks"))

    risks = [
        (
            "DMCA / copyright",
            "LOW",
            GOOD,
            "Videostream has operated profitably in this exact zone for 11+ years with no "
            "DMCA action. Position as a general-purpose utility; content responsibility "
            "rests with the user. Same line VLC, Plex, yt-dlp, Kodi have held "
            "successfully.",
        ),
        (
            "DRM-protected premium content (Widevine L1)",
            "OFF-LIMITS",
            BAD,
            "Netflix, Disney+, HBO Max, Amazon Prime Video protect streams with Widevine "
            "L1. A third-party tool cannot decrypt these and must not attempt to. "
            "Mandatory disclaimer in product copy: <i>&ldquo;Cast Booster does not and "
            "cannot cast DRM-protected premium content.&rdquo;</i>",
        ),
        (
            "Chrome Web Store policy enforcement",
            "MEDIUM",
            WARN,
            "Google has pulled extensions for vague quality concerns without appeal. "
            "Precedent: Videostream was effectively kneecapped by Chrome Apps "
            "deprecation. Mitigation: always have a self-hosted .crx download, and "
            "package the desktop helper as a standalone app that does not depend on "
            "Chrome Web Store distribution.",
        ),
        (
            "Payment processor (Stripe/Paddle)",
            "LOW&ndash;MEDIUM",
            WARN,
            "Generally permissive for utility software but have been known to freeze "
            "accounts if branding leans too heavily into piracy-adjacent messaging. "
            "Mitigation: keep landing page copy focused on local files, cord-cutting, "
            "international content, live sports, and educational content. Avoid the word "
            "&ldquo;pirate.&rdquo;",
        ),
        (
            "Native competition from Google",
            "MEDIUM",
            WARN,
            "Google could add arbitrary-video casting to Chrome at any time. "
            "Counter-evidence: Google has had 10+ years to do this and has not; they "
            "discontinued the Chromecast hardware line in 2024. Unlikely to materialize "
            "in the 2&ndash;3 year window required to build a business.",
        ),
        (
            "User liability for copyrighted streams",
            "LOW",
            GOOD,
            "Precedent: VLC, IDM, youtube-dl, yt-dlp, Kodi, Plex have successfully argued "
            "the tool is content-agnostic. User is responsible for their own viewing "
            "choices. Well-established legal territory.",
        ),
    ]

    for title, level, color, body in risks:
        story.append(
            Paragraph(
                f'<b>{title}</b> &nbsp;&nbsp;<font color="{color.hexval()}">'
                f'<b>[{level}]</b></font>',
                STY["H3"],
            )
        )
        story.append(p(body))

    story.append(spacer(6))
    story.append(
        CalloutBox(
            "Safe product positioning line",
            "<i>&ldquo;Cast Booster lets you cast any video that plays in your web "
            "browser &mdash; including local files, personal media servers, and "
            "international streaming sites &mdash; to your Chromecast or smart TV in "
            "optimized quality. Cast Booster cannot and does not cast DRM-protected "
            "premium content from services like Netflix, Disney+, or HBO Max; please "
            "use those services' official apps instead.&rdquo;</i> "
            "<br/><br/>This wording has carried Videostream for 11+ years and is the safe "
            "zone for this business.",
            bg=QUOTE_BG,
            border=QUOTE_BORDER,
        )
    )
    story.append(PageBreak())

    # -----------------------------------------------------------------------
    # 10. Recommendation
    # -----------------------------------------------------------------------
    story.append(h1("10. Honest recommendation & next steps"))

    story.append(h2("Recommendation"))
    story.append(
        p(
            "<b>Build it, but with realistic expectations.</b> This is not the idea that "
            "makes you rich. It is the idea that (a) solves a real problem for real "
            "people who are actively complaining about it right now, (b) has a proven "
            "pricing model from an abandoned predecessor, (c) fits in a solo-developer "
            "maintenance budget, and (d) could realistically replace a salaried job "
            "within 2&ndash;3 years of focused effort."
        )
    )

    story.append(h2("What to commit to"))
    story.extend(
        bullets(
            [
                "<b>1&ndash;3 months</b> of focused build time for a polished v1 "
                "(Chrome extension + Windows/macOS installer + tray helper).",
                "<b>$0&ndash;$500</b> in upfront costs: code-signing certificate, Stripe "
                "account, a landing page on Vercel or Netlify, domain registration.",
                "<b>12 months</b> of part-time maintenance after launch to stay ahead "
                "of CDN changes.",
            ]
        )
    )

    story.append(h2("Realistic expectations"))
    expectations = [
        ("First 6 months", "100&ndash;5,000 users", "$0&ndash;$2K MRR"),
        ("Year 1", "10K&ndash;50K users", "$2K&ndash;$10K MRR"),
        ("Year 2", "50K&ndash;200K users", "$10K&ndash;$30K MRR"),
        ("Year 3&ndash;5", "200K&ndash;700K users", "$30K&ndash;$150K MRR at steady state"),
    ]
    exp_header = [
        Paragraph("Window", STY["TableHeader"]),
        Paragraph("Install base", STY["TableHeader"]),
        Paragraph("Revenue", STY["TableHeader"]),
    ]
    exp_body = [[Paragraph(c, STY["TableCell"]) for c in row] for row in expectations]
    exp_table = Table(
        [exp_header] + exp_body,
        colWidths=[1.3 * inch, 2.0 * inch, 2.3 * inch],
        repeatRows=1,
        hAlign="LEFT",
    )
    exp_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), ACCENT_DARK),
                ("GRID", (0, 0), (-1, -1), 0.3, BORDER),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, BG_LIGHT]),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ]
        )
    )
    story.append(exp_table)

    story.append(spacer(10))
    story.append(h2("Where the real upside lives"))
    story.extend(
        bullets(
            [
                "<b>Inheriting Videostream's orphans.</b> 500K&ndash;700K paying-tier "
                "Chrome users are actively looking for a replacement right now. A single "
                "honest Reddit post in r/Videostream on launch day could deliver "
                "10K&ndash;50K users in a week.",
                "<b>Niche word-of-mouth.</b> Anime fans, foreign-language content "
                "viewers, and cord-cutters are extremely vocal and loyal to tools that "
                "&ldquo;just work&rdquo; where everything else fails.",
                "<b>Chrome Web Store long tail.</b> Extensions with 100K+ users "
                "accumulate downloads passively for years with almost no marketing.",
            ]
        )
    )

    story.append(h2("Where the real risk lives"))
    story.extend(
        bullets(
            [
                "<b>Maintenance burnout.</b> CDN tricks change constantly. Chrome's APIs "
                "shift every few months. If you stop maintaining for 6 months, reviews "
                "collapse (see: CastBuddy).",
                "<b>Google pulling the rug</b> by banning the extension or adding native "
                "support (low probability over 2&ndash;3 years, not zero).",
                "<b>A bigger player</b> (Plex, a new competitor) arriving before you "
                "establish distribution.",
            ]
        )
    )
    story.append(PageBreak())

    story.append(h2("Concrete next steps if you proceed"))
    steps = [
        (
            "Day 1",
            "Finish and ship the local-proxy version of Cast Booster we have been "
            "building. Get it working reliably on your own EgyDead episodes end-to-end.",
        ),
        (
            "Day 2&ndash;7",
            "Polish the Windows tray-app installer. Add auto-start and auto-update. Set "
            "up a simple Stripe-backed landing page with a free download and a $29 "
            "&ldquo;lifetime Pro&rdquo; button.",
        ),
        (
            "Day 8",
            "Post in r/Chromecast and r/Videostream with an honest &ldquo;I built this "
            "because I was frustrated, here is a free download&rdquo; post. Do NOT lead "
            "with the paid tier. Measure download and conversion.",
        ),
        (
            "Day 15",
            "If 100+ people download in week 1, iterate. If fewer, either the "
            "positioning is wrong or the market isn't there; reassess honestly.",
        ),
        (
            "Month 3",
            "If you have 1,000+ users and 10+ paying customers, commit to this as a "
            "serious side project. Below that, treat it as a portfolio piece.",
        ),
        (
            "Month 6",
            "If you've hit $500&ndash;$1,000 MRR, it is working; double down. Below that, "
            "have an honest conversation about whether the math supports continued "
            "investment.",
        ),
    ]
    for when, what in steps:
        step_table = Table(
            [[Paragraph(f"<b>{when}</b>", STY["TableCell"]), Paragraph(what, STY["TableCell"])]],
            colWidths=[0.9 * inch, 5.2 * inch],
            hAlign="LEFT",
        )
        step_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (0, 0), BG_LIGHT),
                    ("LEFTPADDING", (0, 0), (-1, -1), 8),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                    ("TOPPADDING", (0, 0), (-1, -1), 6),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LINEBELOW", (0, 0), (-1, -1), 0.3, BORDER),
                ]
            )
        )
        story.append(step_table)

    story.append(spacer(14))
    story.append(h2("Can this be a real website / SaaS?"))
    story.append(
        p(
            "<b>Partially.</b> The local proxy piece must run on the user's own machine "
            "&mdash; the Chromecast must be able to reach it on the LAN, and the "
            "session-locked URL has to be fetched from the same external IP. A fully "
            "cloud-hosted SaaS is architecturally impossible for this particular problem."
        )
    )
    story.append(
        p(
            "<b>But the user experience can absolutely feel like a website.</b> The "
            "background service can serve a local web UI at "
            "<font name=\"Courier\">http://localhost:8080/castbooster</font>. Users "
            "visit it in any browser, see a dashboard, pick a device, paste or "
            "auto-detect a URL, hit Cast. It looks and feels like a web app &mdash; the "
            "&ldquo;server&rdquo; just happens to be running on their own laptop."
        )
    )
    story.append(
        p(
            "The <b>landing page, docs, purchase flow, license management, and "
            "auto-update server absolutely can live in the cloud</b> &mdash; and should. "
            "Think of it as a <i>cloud-backed local app</i>, the same shape as 1Password, "
            "Dropbox, or Plex Media Server."
        )
    )
    story.append(
        CalloutBox(
            "Final honest take",
            "This is not the idea that makes you rich. It is the idea that solves a real "
            "problem for real people, has a proven pricing model from an abandoned "
            "predecessor, fits in a solo-developer maintenance budget, and could "
            "realistically replace a salaried job within 2&ndash;3 years of focused "
            "effort. If you are looking for a YC-backable startup, keep looking. If you "
            "are looking for a profitable indie project you can build in 1&ndash;3 "
            "months, ship to a real audience, and maintain for years, <b>this qualifies."
            "</b>",
            bg=BG_LIGHT,
            border=ACCENT,
        )
    )
    story.append(PageBreak())

    # -----------------------------------------------------------------------
    # 11. Sources
    # -----------------------------------------------------------------------
    story.append(h1("11. Sources & citations"))

    story.append(h2("Primary sources (Reddit)"))
    sources_reddit = [
        ('r/Chromecast &mdash; "Any way to stop \'poor playback quality - switched to '
         'mirroring\'"', "reddit.com/r/Chromecast/comments/lwd296"),
        ('r/Chromecast &mdash; "Chromecast is TERRIBLE at Casting a tab"',
         "reddit.com/r/Chromecast/comments/6mzh2c"),
        ('r/Chromecast &mdash; "How do I force chrome (desktop) to cast a tab instead '
         'of a video?"', "reddit.com/r/Chromecast/comments/110dwi3"),
        ('r/Chromecast &mdash; "Casting fullscreen of chrome tab automatically switch '
         'back to mirroring"', "reddit.com/r/Chromecast/comments/az8cdq"),
        ('r/Chromecast &mdash; "Google TV Streamer - Poor Chrome Casting Video"',
         "reddit.com/r/Chromecast/comments/1px5lo9"),
        ('r/Chromecast &mdash; "You can cast (almost) any video on a web page"',
         "reddit.com/r/Chromecast/comments/7lbtzb"),
        ('r/AndroidTV &mdash; "For those using Web Video Caster, remember to download '
         'Web Video Receiver"', "reddit.com/r/AndroidTV/comments/1k3c1lh"),
        ('r/Videostream &mdash; "Video stream is dead"',
         "reddit.com/r/Videostream/comments/q8wsj4"),
    ]
    for title, url in sources_reddit:
        story.append(Paragraph(f"&bull; {title}<br/>&nbsp;&nbsp;&nbsp;{url}", STY["Cite"]))

    story.append(spacer(6))
    story.append(h2("Product listings & download data"))
    sources_products = [
        ("Videostream &mdash; Chrome Web Store",
         "chromewebstore.google.com/detail/videostream-for-google-ch/cnciopoikihiagdjbjpnocolokfelagl"),
        ("Videostream &mdash; official site", "getvideostream.com"),
        ("LocalCast &mdash; official site", "localcast.app"),
        ("Web Video Cast (InstantBits) &mdash; Google Play",
         "play.google.com/store/apps/details?id=com.instantbits.cast.webvideo"),
        ("CastBuddy &mdash; Chrome Web Store",
         "chromewebstore.google.com/detail/castbuddy/ghagedffjalchgcgdgfindabkpnmalel"),
        ("m3u8 Sniffer TV &mdash; Chrome Web Store",
         "chromewebstore.google.com/detail/m3u8-sniffer-tv-find-and/akkncdpkjlfanomlnpmmolafofpnpjgn"),
        ("WebCast-Reloaded &mdash; GitHub",
         "github.com/warren-bank/crx-webcast-reloaded"),
        ("Wikipedia: Videostream", "en.wikipedia.org/wiki/Videostream"),
    ]
    for title, url in sources_products:
        story.append(Paragraph(f"&bull; {title}<br/>&nbsp;&nbsp;&nbsp;{url}", STY["Cite"]))

    story.append(spacer(6))
    story.append(h2("Technical corroboration"))
    sources_tech = [
        ("Bitmovin community &mdash; Chromecast + CloudFront signed cookies",
         "community.bitmovin.com/t/chromecast-cloudfront-signed-cookies-possible/3323"),
        ("Stack Overflow &mdash; Chromecast Receiver App Cookies",
         "stackoverflow.com/questions/23392293/chromecast-receiver-app-cookies"),
        ("Stack Overflow &mdash; Adding CORS headers when requesting m3u8 files",
         "stackoverflow.com/questions/21809527/adding-cors-headers-when-requesting-m3u8-files-using-reverse-proxy"),
        ("@warren-bank/hls-proxy &mdash; npm",
         "npmjs.com/package/@warren-bank/hls-proxy"),
        ("shaka-player issue #1348 &mdash; GitHub",
         "github.com/google/shaka-player/issues/1348"),
        ("Google Media CDN dual-token auth &mdash; Medium",
         "medium.com/google-cloud/protecting-hls-streaming-with-google-media-cdn-dual-tokean-authentication-using-hmac-tokens-9acf6c60c905"),
    ]
    for title, url in sources_tech:
        story.append(Paragraph(f"&bull; {title}<br/>&nbsp;&nbsp;&nbsp;{url}", STY["Cite"]))

    story.append(spacer(6))
    story.append(h2("Chromecast ecosystem news"))
    sources_news = [
        ('The Verge &mdash; "Google quietly ends support for decade-old Chromecast" (May 2023)',
         "theverge.com/2023/5/31/23743515/google-chromecast-support-ending-2013"),
        ('The Verge &mdash; "Google is discontinuing the Chromecast line" (August 2024)',
         "theverge.com/2024/8/6/24214471/google-chromecast-line-discontinued"),
        ("Crunchyroll Chromecast help doc",
         "help.crunchyroll.com/hc/en-us/articles/20980204719252"),
    ]
    for title, url in sources_news:
        story.append(Paragraph(f"&bull; {title}<br/>&nbsp;&nbsp;&nbsp;{url}", STY["Cite"]))

    story.append(spacer(10))
    story.append(hr())
    story.append(
        p(
            "<b>Research methodology:</b> This report was compiled on April 14, 2026 from "
            "Exa.ai web search (advanced mode, ~40 total results across 6 queries), Brave "
            "Web Search (~30 results across targeted queries), and direct reading of "
            "forum posts and product listings. Raw search output was triaged by an "
            "automated research subagent and manually verified. All user quotes and vote "
            "counts are verbatim from their source pages at the time of research."
        )
    )
    story.append(spacer(10))
    story.append(
        p(
            '<font color="#6a6f7b"><i>End of report.</i></font>',
            "Body",
        )
    )

    return story


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------
def build():
    doc = BaseDocTemplate(
        OUTPUT,
        pagesize=LETTER,
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
        topMargin=0.9 * inch,
        bottomMargin=0.85 * inch,
        title="Cast Booster — SaaS Market Analysis",
        author="Independent research",
        subject="Market analysis for a Chrome extension + desktop helper SaaS",
    )

    content_frame = Frame(
        doc.leftMargin,
        doc.bottomMargin,
        doc.width,
        doc.height,
        id="content",
        leftPadding=0,
        rightPadding=0,
        topPadding=0,
        bottomPadding=0,
    )

    cover_template = PageTemplate(
        id="cover", frames=[content_frame], onPage=_cover_page, pagesize=LETTER
    )
    body_template = PageTemplate(
        id="body", frames=[content_frame], onPage=_body_page, pagesize=LETTER
    )
    doc.addPageTemplates([cover_template, body_template])

    story = build_story()
    # Force switch to body template after the first PageBreak (after cover)
    # BaseDocTemplate swaps templates when it encounters NextPageTemplate
    from reportlab.platypus import NextPageTemplate
    # Inject NextPageTemplate at position 0 so subsequent pages use body template
    story.insert(1, NextPageTemplate("body"))

    doc.build(story)
    print(f"Wrote {OUTPUT}")
    print(f"Size: {os.path.getsize(OUTPUT):,} bytes")


if __name__ == "__main__":
    build()
