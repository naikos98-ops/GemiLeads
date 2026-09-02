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
      borderRadius: {
        chip: 'var(--radius-sm)',
        control: 'var(--radius-lg)',
        panel: 'var(--radius-md)',
        card: 'var(--radius-xl)',
      },
      boxShadow: {
        // Was 0 24px 80px: an 80px blur offset 24px down, applied to 58 elements
        // including flat inline ones. At that size it reads as a template default
        // rather than elevation. A two-layer shadow at a realistic distance keeps
        // cards lifted off the cream ground without the haze.
        soft: '0 1px 2px rgba(7,23,37,.06), 0 8px 24px rgba(7,23,37,.08)',
        glow: '0 0 32px rgba(33,102,224,.28)',
      },
      // `float` was never referenced by any template, and `shine` drove an infinite
      // sweep across the primary hero CTA -- perpetual motion on the one button the
      // page most wants read. Both removed; emphasis now comes from hover only.
    },
  },
  plugins: [],
};
