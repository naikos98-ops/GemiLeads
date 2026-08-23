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
      boxShadow: {
        soft: '0 24px 80px rgba(7,23,37,.12)',
        glow: '0 0 40px rgba(40,120,255,.22)',
      },
      keyframes: {
        float: { '0%,100%': { transform: 'translateY(0)' }, '50%': { transform: 'translateY(-12px)' } },
        shine: { '0%': { transform: 'translateX(-150%) skewX(-18deg)' }, '100%': { transform: 'translateX(250%) skewX(-18deg)' } },
      },
      animation: {
        float: 'float 6s ease-in-out infinite',
        shine: 'shine 2.8s ease-in-out infinite',
      },
    },
  },
  plugins: [],
};
