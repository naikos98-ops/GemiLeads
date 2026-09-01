"""WCAG AA contrast guard for the compiled stylesheet.

The landing page once shipped dark-navy body copy (text-navy-700/65) onto the
dark navy cards used by the phone dark theme -- 1.36:1, effectively invisible.
The same class was only 3.69:1 on white, so light mode failed AA too.

Both modes are now driven by the text tokens in static/src/input.css. These
tests fail if a text utility loses its token binding or if any token drops
below 4.5:1 on a surface it can land on, in either mode.

Run against the BUILT stylesheet, so `npm run build:css` must have been run.
"""

import re
from pathlib import Path

from django.test import SimpleTestCase

CSS_PATH = Path(__file__).resolve().parent.parent / "static" / "css" / "app.css"

# Surfaces a token-driven text run can land on, per mode.
LIGHT_SURFACES = {
    "white": "#ffffff", "sand-50": "#fbf8f1",
    "sand-100": "#f3ecdf", "sand-200": "#e6d8c3",
}
DARK_SURFACES = {
    "card": "#10263c", "page": "#0a1a2b",
    "sunk": "#0e2135", "raised": "#143050",
}
# The navy chrome (navbar, hero, dark CTA panels) is dark in BOTH modes.
NAVY_SURFACES = {"navy-950": "#071725", "navy-900": "#0b2239", "navy-800": "#123453"}

ON_NAVY_TOKENS = {
    "--text-on-navy", "--text-on-navy-muted", "--text-on-navy-link",
    "--text-on-navy-success", "--text-on-navy-accent",
}

AA_NORMAL = 4.5

# Every text utility the templates use must map to a token. Losing an entry
# here means that class falls back to its raw Tailwind shade.
REQUIRED_BINDINGS = [
    "text-navy-950", "text-navy-900", "text-navy-800", "text-navy-700",
    "text-navy-900/80", "text-navy-900/75", "text-navy-900/70",
    "text-navy-700/80", "text-navy-700/70", "text-navy-700/65",
    "text-navy-900/60", "text-navy-700/60", "text-navy-700/55",
    "text-navy-700/50", "text-navy-700/45", "text-navy-700/40",
    "text-navy-700/35", "text-navy-700/30",
    "text-blue-900", "text-blue-800", "text-blue-700", "text-blue-600",
    "text-blue-400", "text-blue-300", "text-blue-200", "text-blue-100",
    "text-signal",
    "text-emerald-900", "text-emerald-700", "text-emerald-600", "text-emerald-400",
    "text-amber-900", "text-amber-800", "text-amber-700", "text-amber-600",
    "text-amber-500",
    "text-red-900", "text-red-700", "text-red-600",
    "text-purple-950", "text-purple-900", "text-purple-700",
    "text-purple-400", "text-purple-300", "text-purple-200",
    "text-white",
]


def _channel(value):
    value = value / 255
    return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4


def _luminance(rgb):
    r, g, b = (_channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _hex_to_rgb(value):
    value = value.lstrip("#").strip()
    if len(value) == 3:
        value = "".join(c * 2 for c in value)
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def _composite(fg, bg, alpha):
    return tuple(fg[i] * alpha + bg[i] * (1 - alpha) for i in range(3))


def contrast(fg_rgb, bg_rgb):
    l1, l2 = _luminance(fg_rgb), _luminance(bg_rgb)
    if l1 < l2:
        l1, l2 = l2, l1
    return (l1 + 0.05) / (l2 + 0.05)


def parse_color(value):
    """Accept the hex / rgb() / hsla() forms the minifier can emit."""
    import colorsys

    value = value.strip()
    if value.startswith("#"):
        return _hex_to_rgb(value), 1.0
    m = re.match(r"rgba?\((\d+)[,\s]+(\d+)[,\s]+(\d+)(?:\s*[,/]\s*([\d.]+))?\)", value)
    if m:
        return (int(m[1]), int(m[2]), int(m[3])), float(m[4] or 1)
    m = re.match(r"hsla?\(\s*([\d.]+),\s*([\d.]+)%,\s*([\d.]+)%(?:,\s*([\d.]+))?\)", value)
    if m:
        r, g, b = colorsys.hls_to_rgb(float(m[1]) / 360, float(m[3]) / 100, float(m[2]) / 100)
        return (r * 255, g * 255, b * 255), float(m[4] or 1)
    raise AssertionError("unparseable colour: %r" % value)


class ContrastTokenTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.css = CSS_PATH.read_text(encoding="utf-8")

        def token_map(scope):
            return {
                "--%s-%s" % (m[1], m[2]): m[3].strip()
                for m in re.finditer(r"--(text|surface|border)-([a-z-]+):\s*([^;}]+)", scope)
            }

        root = re.search(r":root\{([^}]*)\}", cls.css)
        assert root, "no :root token block in the compiled CSS"
        cls.light = token_map(root[1])

        dark = re.search(
            r"@media\s*\(max-width:\s*900px\)\s*and\s*"
            r"\(prefers-color-scheme:\s*dark\)\{:root\{([^}]*)\}",
            cls.css,
        )
        assert dark, "no dark-mode token override block in the compiled CSS"
        cls.dark = dict(cls.light)
        cls.dark.update(token_map(dark[1]))

        cls.bindings = {}
        for m in re.finditer(r"([^{}]*?)\{color:var\((--text-[a-z-]+)\)!important\}", cls.css):
            for sel in m[1].split(","):
                sel = sel.strip()
                if sel.startswith(".text-"):
                    cls.bindings[sel[1:].replace("\\", "")] = m[2]

    def test_stylesheet_is_built(self):
        self.assertTrue(
            CSS_PATH.exists(),
            "static/css/app.css is missing -- run `npm run build:css`",
        )

    def test_every_text_utility_is_bound_to_a_token(self):
        missing = [c for c in REQUIRED_BINDINGS if c not in self.bindings]
        self.assertEqual(
            missing, [],
            "these text utilities are not bound to a token and fall back to "
            "their raw Tailwind shade: %s" % missing,
        )

    def test_no_binding_points_at_an_undefined_token(self):
        for cls_name, token in sorted(self.bindings.items()):
            for mode, values in (("light", self.light), ("dark", self.dark)):
                self.assertIn(
                    token, values,
                    "%s -> %s is undefined in %s mode" % (cls_name, token, mode),
                )

    def test_all_bound_classes_meet_wcag_aa_in_both_modes(self):
        failures = []
        for cls_name, token in sorted(self.bindings.items()):
            on_navy = token in ON_NAVY_TOKENS
            for mode, values in (("light", self.light), ("dark", self.dark)):
                surfaces = NAVY_SURFACES if on_navy else (
                    LIGHT_SURFACES if mode == "light" else DARK_SURFACES
                )
                fg, alpha = parse_color(values[token])
                for surface_name, surface_hex in surfaces.items():
                    bg = _hex_to_rgb(surface_hex)
                    ratio = contrast(_composite(fg, bg, alpha), bg)
                    if ratio < AA_NORMAL:
                        failures.append(
                            "%s (%s) %s on %s: %.2f:1"
                            % (cls_name, token, mode, surface_name, ratio)
                        )
        self.assertEqual(
            failures, [],
            "text below WCAG AA (%.1f:1):\n  %s" % (AA_NORMAL, "\n  ".join(failures)),
        )

    def test_body_copy_is_readable_on_dark_cards(self):
        """Regression guard for the reported bug specifically."""
        token = self.bindings["text-navy-700/65"]
        fg, alpha = parse_color(self.dark[token])
        card = _hex_to_rgb(DARK_SURFACES["card"])
        ratio = contrast(_composite(fg, card, alpha), card)
        self.assertGreaterEqual(
            ratio, AA_NORMAL,
            "landing-page body copy is unreadable on dark cards (%.2f:1)" % ratio,
        )

    def test_dark_headings_are_not_pure_white(self):
        """The hierarchy must stay a ramp, not flatten to #fff."""
        strong = parse_color(self.dark["--text-strong"])[0]
        self.assertNotEqual(
            tuple(round(c) for c in strong), (255, 255, 255),
            "dark headings collapsed to pure white; keep a near-white token",
        )
        ramp = [
            _luminance(parse_color(self.dark[t])[0])
            for t in ("--text-strong", "--text-body", "--text-muted", "--text-faint")
        ]
        self.assertEqual(
            ramp, sorted(ramp, reverse=True),
            "dark text tokens must descend strong > body > muted > faint",
        )
