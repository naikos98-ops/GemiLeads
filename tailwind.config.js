/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    "./templates/**/*.html",
    "./static/js/**/*.js",
    "./gemiapp/**/*.py",
  ],
  theme: {
    extend: {
      colors: {
        navy: { 950: '#071725', 900: '#0b2239', 800: '#123453', 700: '#1b496c' },
        sand: { 50: '#fbf8f1', 100: '#f3ecdf', 200: '#e6d8c3', 300: '#d8c2a2' },
        signal: '#2878ff',
      },
      fontFamily: { sans: ['Inter', 'ui-sans-serif', 'system-ui', 'sans-serif'] },
      // base.html used h-18 for the fixed nav, which Tailwind never generated. main compensates
      // with pt-18, so the two must come from the same value.
      spacing: { 18: '4.5rem' },
      // Semantic radius scale, defined as tokens in static/src/input.css. Templates use
      // rounded-card / rounded-panel / rounded-control / rounded-chip instead of the nine
      // ad-hoc values (rounded-[2rem], [1.75rem], [1.6rem], [1.5rem], 3xl, 2xl, xl, lg, md)
      // they had grown into. rounded-full is unchanged and still correct for pills.
      // `card` and `chip` are gone: the product, public and admin surfaces are all on the
      // Signal Ledger system now (static/css/product-ui.css), which uses square corners and
      // thin rules rather than a rounded-card system. Nothing references them any more, and
      // removing the keys means a future `rounded-card` silently fails to generate instead of
      // quietly reintroducing the old SaaS look. `control` and `panel` remain because
      // includes/kad_picker.html and static/js/app.js still use them.
      borderRadius: {
        control: 'var(--radius-lg)',
        panel: 'var(--radius-md)',
      },
      // `glow` is gone with the blue SaaS CTAs that used it. `soft` survives only for
      // includes/kad_picker.html, which renders inside the approved Signals and Radar-form
      // screens and is therefore deliberately left alone. Do not reach for either on a new
      // surface -- the Signal Ledger system is flat.
      boxShadow: {
        soft: '0 1px 2px rgba(7,23,37,.06), 0 8px 24px rgba(7,23,37,.08)',
      },
      // `float` was never referenced by any template, and `shine` drove an infinite
      // sweep across the primary hero CTA -- perpetual motion on the one button the
      // page most wants read. Both removed; emphasis now comes from hover only.
    },
  },
  plugins: [],
};
