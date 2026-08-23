# Performance — Findings

No CrUX/GSC field data available. LCP/INP/CLS deliberately NOT quantified.

## Measured (live)
| Metric | Value |
|---|---|
| TTFB run 1 | 1.279 s |
| TTFB run 2 | 0.816 s |
| TTFB run 3 | 0.834 s |
| DNS | <20 ms |
| TCP connect | <30 ms |
| HTML (gzip) | 6.1 KB |
| Tailwind CDN JS | 120.4 KB (render-blocking) |
| favicon.png | 129.2 KB |
| app.js | 3.4 KB |
| Critical path total | 259.1 KB |

Latency is entirely server-side think time (connect is fast, TTFB is slow).

## Causes
- P1: Tailwind Play CDN = 120 KB JIT compiler executing on-device. Fixed in 446a28f.
- P2: uncached Company.objects.count() in context_processors.py on EVERY request.
- P3: render-blocking Google Fonts, 5 weights.
- T4: static assets cached only 60 s.
