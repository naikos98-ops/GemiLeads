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


# Every LIGHT-valued bg-* utility a non-superadmin, non-email template uses must
# be remapped by the phone dark block. One that is not keeps its light value
# while the text on top is lightened -- the same failure as the original
# text-navy-700/65 bug, arriving from the surface side. Before this guard eight
# of these were uncovered and measured 1.28:1 to 1.83:1, the worst being the
# cookie consent banner every new mobile visitor meets first.
#
# Always-dark chrome (bg-navy-*, bg-white/10 and friends on the navbar, hero and
# dark CTA panels) is deliberately absent: those surfaces do not flip, and the
# --text-on-navy-* family already covers the text on them.
REQUIRED_SURFACE_BINDINGS = [
    "bg-white", "bg-white/95", "bg-white/90", "bg-white/80", "bg-white/70",
    "bg-sand-50", "bg-sand-100", "bg-sand-200", "bg-sand-50/60",
    "bg-blue-50", "bg-blue-50/60", "bg-blue-50/70", "bg-blue-100", "bg-blue-200",
    "bg-emerald-50", "bg-emerald-100",
    "bg-amber-50", "bg-amber-100",
    "bg-red-50", "bg-red-100",
    "bg-purple-50",
]

# Which text token each semantic tint has to carry. A success badge is only
# useful if its green text is legible on its green ground.
TINT_TEXT_PAIRS = {
    "--surface-tint-info": "--text-link",
    "--surface-tint-success": "--text-success",
    "--surface-tint-warning": "--text-warning",
    "--surface-tint-danger": "--text-danger",
    "--surface-tint": "--text-body",
}


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

        # Surface bindings, scoped to the phone dark block. Slicing from the
        # media query keeps a light-mode background rule from being mistaken
        # for dark-mode coverage.
        dark_block = cls.css[dark.start():]
        cls.surface_bindings = {}
        for m in re.finditer(
            r"([^{}]*?)\{background-color:var\((--surface-[a-z-]+)\)!important\}", dark_block
        ):
            for sel in m[1].split(","):
                sel = sel.strip()
                if sel.startswith(".bg-"):
                    cls.surface_bindings[sel[1:].replace("\\", "")] = m[2]

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

    def test_every_light_surface_is_remapped_in_dark_mode(self):
        """The surface-side counterpart of the text binding guard.

        A light bg-* left uncovered keeps its light value on a dark page while
        the text on it is lightened -- the exact bug this file was written for,
        inverted.
        """
        missing = [c for c in REQUIRED_SURFACE_BINDINGS if c not in self.surface_bindings]
        self.assertEqual(
            missing, [],
            "these light surfaces are not remapped by the dark block, so text "
            "on them will be lightened against a light ground: %s" % missing,
        )

    def test_no_surface_binding_points_at_an_undefined_token(self):
        for cls_name, token in sorted(self.surface_bindings.items()):
            self.assertIn(
                token, self.dark,
                "%s -> %s is undefined in dark mode" % (cls_name, token),
            )

    def test_semantic_tints_carry_their_text_at_aa(self):
        """A tint must keep its hue AND stay legible on every ground it sits on."""
        failures = []
        for mode, values in (("light", self.light), ("dark", self.dark)):
            grounds = LIGHT_SURFACES if mode == "light" else DARK_SURFACES
            for tint_token, text_token in TINT_TEXT_PAIRS.items():
                if tint_token not in values:
                    continue
                tint, tint_alpha = parse_color(values[tint_token])
                fg, fg_alpha = parse_color(values[text_token])
                for ground_name, ground_hex in grounds.items():
                    ground = _hex_to_rgb(ground_hex)
                    # The tint is a translucent wash: composite it first, then
                    # the text on the resulting surface.
                    surface = _composite(tint, ground, tint_alpha)
                    ratio = contrast(_composite(fg, surface, fg_alpha), surface)
                    if ratio < AA_NORMAL:
                        failures.append(
                            "%s + %s %s on %s: %.2f:1"
                            % (tint_token, text_token, mode, ground_name, ratio)
                        )
        self.assertEqual(
            failures, [],
            "semantic tint below WCAG AA (%.1f:1):\n  %s"
            % (AA_NORMAL, "\n  ".join(failures)),
        )

    def test_cookie_banner_surface_is_readable(self):
        """Regression guard for the worst failure found in the Phase 1 audit.

        bg-white/95 measured 1.28:1 in phone dark mode. It is the consent
        banner, so an unreadable one suppresses accepts and with them all
        analytics -- a business bug as much as an accessibility one.
        """
        self.assertIn(
            "bg-white/95", self.surface_bindings,
            "the cookie consent banner surface is not remapped in dark mode",
        )
        token = self.surface_bindings["bg-white/95"]
        surface, surface_alpha = parse_color(self.dark[token])
        page = _hex_to_rgb(DARK_SURFACES["page"])
        surface = _composite(surface, page, surface_alpha)
        fg, alpha = parse_color(self.dark["--text-body"])
        ratio = contrast(_composite(fg, surface, alpha), surface)
        self.assertGreaterEqual(
            ratio, AA_NORMAL,
            "cookie consent banner text is unreadable in dark mode (%.2f:1)" % ratio,
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
