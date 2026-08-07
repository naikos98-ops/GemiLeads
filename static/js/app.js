(() => {
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const reveal = () => document.querySelectorAll('[data-reveal]').forEach((el, index) => {
    if (reduced) return el.classList.add('visible');
    const observer = new IntersectionObserver(([entry]) => {
      if (entry.isIntersecting) {
        el.style.transitionDelay = `${Math.min(index % 5, 4) * 80}ms`;
        el.classList.add('visible'); observer.disconnect();
      }
    }, { threshold: .12 }); observer.observe(el);
  });
  const counters = () => document.querySelectorAll('[data-counter]').forEach(el => {
    const target = Number(el.dataset.counter || 0); if (reduced) return el.textContent = target.toLocaleString('el-GR');
    const start = performance.now(), duration = 900;
    const tick = now => { const p = Math.min((now - start) / duration, 1); const eased = 1 - Math.pow(1 - p, 3); el.textContent = Math.floor(target * eased).toLocaleString('el-GR'); if (p < 1) requestAnimationFrame(tick); };
    requestAnimationFrame(tick);
  });
  window.renderSignalChart = () => {
    const root = document.querySelector('[data-chart]'); if (!root) return;
    let data = []; try { data = JSON.parse(root.dataset.chart || '[]'); } catch (_) {}
    const max = Math.max(...data.map(x => x.total), 1);
    if (!data.length) { root.innerHTML = '<p class="m-auto text-sm text-navy-700/40">Τα δεδομένα του γραφήματος θα εμφανιστούν μετά την πρώτη εισαγωγή.</p>'; return; }
    root.innerHTML = data.map((x, i) => `<div class="group flex h-full flex-1 flex-col justify-end gap-2"><div class="relative flex-1"><div class="absolute bottom-0 w-full rounded-t-xl bg-gradient-to-t from-blue-600 to-blue-400 transition-all duration-700 group-hover:from-navy-800 group-hover:to-blue-500" style="height:${Math.max(x.total / max * 100, 6)}%;transition-delay:${i*70}ms"><span class="absolute -top-7 left-1/2 -translate-x-1/2 text-xs font-bold opacity-0 transition group-hover:opacity-100">${x.total}</span></div></div><span class="text-center text-[10px] text-navy-700/45">${x.date}</span></div>`).join('');
  };
  document.getElementById('menuButton')?.addEventListener('click', () => document.getElementById('mobileMenu')?.classList.toggle('hidden'));
  setTimeout(() => document.querySelectorAll('[data-toast]').forEach(x => { x.style.opacity = '0'; setTimeout(() => x.remove(), 300); }), 3500);
  document.addEventListener('DOMContentLoaded', () => { reveal(); counters(); window.renderSignalChart(); });
})();
