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
    // The bar track has an explicit height (h-32 = 128px). The previous version put the bars
    // in a `flex-1` div whose only child was absolutely positioned, so it collapsed to 0px
    // and every bar rendered at height:X% of 0 -- i.e. invisible.
    root.innerHTML = data.map((x, i) => `<div class="group flex h-full flex-1 flex-col justify-end gap-2"><div class="relative h-32 w-full"><div class="absolute bottom-0 w-full rounded-t-xl bg-gradient-to-t from-blue-600 to-blue-400 transition-all duration-700 group-hover:from-navy-800 group-hover:to-blue-500" style="height:${Math.max(x.total / max * 100, 4)}%;transition-delay:${i*70}ms"><span class="absolute -top-7 left-1/2 -translate-x-1/2 text-xs font-bold opacity-0 transition group-hover:opacity-100">${x.total}</span></div></div><span class="text-center text-[10px] text-navy-700/45">${x.date}</span></div>`).join('');
  };
  const sizeCompanyTable = () => {
    const scroller = document.querySelector('[data-company-scroll]'); if (!scroller) return;
    const header = scroller.querySelector('thead');
    const rows = [...scroller.querySelectorAll('tbody tr')].slice(0, 20);
    if (rows.length < 20) { scroller.style.maxHeight = 'none'; return; }
    const height = (header?.getBoundingClientRect().height || 0) + rows.reduce((sum, row) => sum + row.getBoundingClientRect().height, 0);
    scroller.style.maxHeight = `${Math.ceil(height)}px`;
  };
  const initKadPickers = () => document.querySelectorAll('[data-kad-picker]').forEach(root => {
    const input = root.querySelector('[data-kad-search]');
    const results = root.querySelector('[data-kad-results]');
    const selectedRoot = root.querySelector('[data-kad-selected]');
    const status = root.querySelector('[data-kad-status]');
    const spinner = root.querySelector('[data-kad-spinner]');
    const inputName = root.dataset.inputName;
    const maxItems = Number(root.dataset.maxItems || 25);
    let timer, controller, activeIndex = -1;

    const selectedCodes = () => new Set([...selectedRoot.querySelectorAll('input[type="hidden"]')].map(item => item.value));
    const updateStatus = (message = '') => {
      const count = selectedCodes().size;
      status.textContent = message || (count ? `${count} επιλεγμένοι ΚΑΔ` : 'Χωρίς επιλογή εμφανίζονται όλοι οι κλάδοι.');
    };
    const closeResults = () => {
      results.classList.add('hidden'); results.replaceChildren(); activeIndex = -1; input.setAttribute('aria-expanded', 'false');
    };
    const resultButtons = () => [...results.querySelectorAll('[role="option"]')];
    const setActive = index => {
      const buttons = resultButtons(); if (!buttons.length) return;
      activeIndex = (index + buttons.length) % buttons.length;
      buttons.forEach((button, i) => {
        button.classList.toggle('bg-blue-50', i === activeIndex);
        button.setAttribute('aria-selected', i === activeIndex ? 'true' : 'false');
      });
      buttons[activeIndex].scrollIntoView({ block: 'nearest' });
    };
    const createChip = item => {
      if (selectedCodes().has(item.normalized_code)) { updateStatus('Ο ΚΑΔ είναι ήδη επιλεγμένος.'); return; }
      if (selectedCodes().size >= maxItems) { updateStatus(`Μπορείς να επιλέξεις έως ${maxItems} ΚΑΔ.`); return; }
      const chip = document.createElement('span');
      chip.dataset.kadChip = ''; chip.dataset.code = item.normalized_code;
      chip.className = 'inline-flex max-w-full items-center gap-2 rounded-full bg-blue-50 px-3 py-2 text-xs font-semibold text-blue-800';
      const label = document.createElement('span'); label.className = 'truncate';
      const strong = document.createElement('b'); strong.textContent = item.code;
      label.append(strong, document.createTextNode(` · ${item.description}`));
      const remove = document.createElement('button'); remove.type = 'button'; remove.dataset.kadRemove = '';
      remove.className = 'grid h-5 w-5 shrink-0 place-items-center rounded-full bg-blue-100 text-sm hover:bg-blue-200';
      remove.setAttribute('aria-label', `Αφαίρεση ΚΑΔ ${item.code}`); remove.textContent = '×';
      const hidden = document.createElement('input'); hidden.type = 'hidden'; hidden.name = inputName; hidden.value = item.normalized_code;
      chip.append(label, remove, hidden); selectedRoot.append(chip);
      input.value = ''; closeResults(); updateStatus(); input.focus();
    };
    const renderResults = items => {
      results.replaceChildren();
      const available = items.filter(item => !selectedCodes().has(item.normalized_code));
      if (!available.length) {
        const empty = document.createElement('p'); empty.className = 'px-4 py-5 text-center text-sm text-navy-700/45';
        empty.textContent = 'Δεν βρέθηκαν άλλοι ΚΑΔ.'; results.append(empty);
      } else available.forEach(item => {
        const button = document.createElement('button'); button.type = 'button'; button.setAttribute('role', 'option');
        button.className = 'flex w-full items-start justify-between gap-4 rounded-panel px-4 py-3 text-left transition hover:bg-blue-50';
        const copy = document.createElement('span'); copy.className = 'min-w-0';
        const code = document.createElement('b'); code.className = 'block text-sm text-blue-700'; code.textContent = item.code;
        const description = document.createElement('span'); description.className = 'mt-1 block text-xs leading-5 text-navy-700/60'; description.textContent = item.description;
        const action = document.createElement('span'); action.className = 'shrink-0 rounded-full bg-sand-100 px-2 py-1 text-[10px] font-bold text-navy-700/60'; action.textContent = 'Προσθήκη';
        copy.append(code, description); button.append(copy, action); button.addEventListener('click', () => createChip(item)); results.append(button);
      });
      results.classList.remove('hidden'); input.setAttribute('aria-expanded', 'true'); activeIndex = -1;
    };
    const search = query => {
      controller?.abort(); controller = new AbortController(); spinner.classList.remove('hidden');
      const url = new URL(root.dataset.searchUrl, window.location.origin); url.searchParams.set('q', query);
      fetch(url, { signal: controller.signal, headers: { Accept: 'application/json' } })
        .then(response => { if (!response.ok) throw new Error('search'); return response.json(); })
        .then(data => renderResults(data.results || []))
        .catch(error => { if (error.name !== 'AbortError') updateStatus('Η αναζήτηση δεν ήταν διαθέσιμη. Δοκίμασε ξανά.'); })
        .finally(() => spinner.classList.add('hidden'));
    };
    input.addEventListener('input', () => {
      clearTimeout(timer); const query = input.value.trim();
      if (query.length < 2) { closeResults(); updateStatus(query ? 'Γράψε τουλάχιστον 2 χαρακτήρες.' : ''); return; }
      timer = setTimeout(() => search(query), 180);
    });
    input.addEventListener('keydown', event => {
      const buttons = resultButtons();
      if (event.key === 'ArrowDown' && buttons.length) { event.preventDefault(); setActive(activeIndex + 1); }
      else if (event.key === 'ArrowUp' && buttons.length) { event.preventDefault(); setActive(activeIndex - 1); }
      else if (event.key === 'Enter' && activeIndex >= 0) { event.preventDefault(); buttons[activeIndex].click(); }
      else if (event.key === 'Escape') closeResults();
    });
    selectedRoot.addEventListener('click', event => {
      const remove = event.target.closest('[data-kad-remove]'); if (!remove) return;
      remove.closest('[data-kad-chip]')?.remove(); updateStatus();
    });
    document.addEventListener('click', event => { if (!root.contains(event.target)) closeResults(); });
  });
  // Searchable multi-select over the canonical GEMI reference data, used by the Organization Radar criteria
  // (templates/includes/reference_picker.html). Progressive enhancement: the plain textarea / <select multiple>
  // is what posts without JavaScript, so here it is hidden AND disabled -- a disabled control is not submitted --
  // and the chips take over. The posted representation is unchanged: `lines` keeps the newline text the server
  // already parses, otherwise one hidden input per chip carries the reference primary key.
  const initReferencePickers = () => document.querySelectorAll('[data-reference-picker]').forEach(root => {
    const input = root.querySelector('[data-picker-search]');
    const results = root.querySelector('[data-picker-results]');
    const chips = root.querySelector('[data-picker-selected]');
    const status = root.querySelector('[data-picker-status]');
    const spinner = root.querySelector('[data-picker-spinner]');
    const fallback = root.querySelector('[data-picker-fallback]');
    const lines = root.querySelector('[data-picker-lines]');
    const select = root.querySelector('[data-picker-select]');
    const { kind, inputName, emit, searchUrl } = root.dataset;
    const maxItems = Number(root.dataset.maxItems || 50);
    if (!input || !results || !chips) return;
    let timer, controller, activeIndex = -1;

    // Take over from the no-JavaScript control.
    if (fallback) fallback.hidden = true;
    if (select) select.disabled = true;
    chips.querySelectorAll('[data-picker-value]').forEach(hidden => { hidden.disabled = false; });

    const chosen = () => [...chips.querySelectorAll('[data-picker-chip]')];
    const values = () => new Set(chosen().map(chip => chip.dataset.value));
    const sync = () => {
      if (emit === 'lines' && lines) lines.value = chosen().map(chip => chip.dataset.value).join('\n');
      const count = chosen().length;
      status.textContent = count ? `${count} επιλεγμένα` : '';
    };
    const closeResults = () => {
      results.hidden = true; results.replaceChildren(); activeIndex = -1;
      input.setAttribute('aria-expanded', 'false');
    };
    const options = () => [...results.querySelectorAll('[role="option"]')];
    const setActive = index => {
      const items = options(); if (!items.length) return;
      activeIndex = (index + items.length) % items.length;
      items.forEach((item, i) => {
        item.classList.toggle('is-active', i === activeIndex);
        item.setAttribute('aria-selected', i === activeIndex ? 'true' : 'false');
      });
      items[activeIndex].scrollIntoView({ block: 'nearest' });
    };
    const addChip = item => {
      if (values().has(item.value)) { status.textContent = 'Είναι ήδη επιλεγμένο.'; return; }
      if (chosen().length >= maxItems) { status.textContent = `Μπορείς να επιλέξεις έως ${maxItems}.`; return; }
      const chip = document.createElement('span');
      chip.className = 'product-picker-chip'; chip.dataset.pickerChip = ''; chip.dataset.value = item.value;
      const label = document.createElement('span');
      label.className = 'product-picker-chip-label'; label.textContent = item.label;
      chip.append(label);
      if (item.detail) { const tag = document.createElement('em'); tag.textContent = item.detail; chip.append(tag); }
      const remove = document.createElement('button');
      remove.type = 'button'; remove.dataset.pickerRemove = '';
      remove.setAttribute('aria-label', `Αφαίρεση: ${item.label}`); remove.textContent = '×';
      chip.append(remove);
      if (emit !== 'lines') {
        const hidden = document.createElement('input');
        hidden.type = 'hidden'; hidden.name = inputName; hidden.value = item.value; hidden.dataset.pickerValue = '';
        chip.append(hidden);
      }
      chips.append(chip); input.value = ''; closeResults(); sync(); input.focus();
    };
    const render = items => {
      results.replaceChildren();
      const available = items.filter(item => !values().has(item.value));
      if (!available.length) {
        const empty = document.createElement('p');
        empty.className = 'product-picker-empty'; empty.textContent = 'Δεν βρέθηκε κάτι άλλο.';
        results.append(empty);
      } else available.forEach(item => {
        const button = document.createElement('button');
        button.type = 'button'; button.setAttribute('role', 'option'); button.setAttribute('aria-selected', 'false');
        button.className = 'product-picker-option';
        const label = document.createElement('span'); label.textContent = item.label;
        button.append(label);
        if (item.detail) { const tag = document.createElement('em'); tag.textContent = item.detail; button.append(tag); }
        button.addEventListener('click', () => addChip(item));
        results.append(button);
      });
      results.hidden = false; input.setAttribute('aria-expanded', 'true'); activeIndex = -1;
    };
    const search = query => {
      controller?.abort(); controller = new AbortController();
      if (spinner) spinner.hidden = false;
      const url = new URL(searchUrl, window.location.origin);
      url.searchParams.set('kind', kind); url.searchParams.set('q', query);
      fetch(url, { signal: controller.signal, headers: { Accept: 'application/json' } })
        .then(response => { if (!response.ok) throw new Error('search'); return response.json(); })
        .then(data => render(data.results || []))
        .catch(error => { if (error.name !== 'AbortError') status.textContent = 'Η αναζήτηση δεν ήταν διαθέσιμη.'; })
        .finally(() => { if (spinner) spinner.hidden = true; });
    };
    input.addEventListener('input', () => {
      clearTimeout(timer);
      const query = input.value.trim();
      if (query.length < 2) { closeResults(); sync(); return; }
      timer = setTimeout(() => search(query), 180);
    });
    input.addEventListener('keydown', event => {
      const items = options();
      if (event.key === 'ArrowDown' && items.length) { event.preventDefault(); setActive(activeIndex + 1); }
      else if (event.key === 'ArrowUp' && items.length) { event.preventDefault(); setActive(activeIndex - 1); }
      else if (event.key === 'Enter' && activeIndex >= 0) { event.preventDefault(); items[activeIndex].click(); }
      else if (event.key === 'Escape') closeResults();
      else if (event.key === 'Enter') event.preventDefault();   // never submit the form from the search box
    });
    chips.addEventListener('click', event => {
      const remove = event.target.closest('[data-picker-remove]'); if (!remove) return;
      remove.closest('[data-picker-chip]')?.remove(); sync();
    });
    document.addEventListener('click', event => { if (!root.contains(event.target)) closeResults(); });
    sync();
  });
  // Fires a GA4 event when a tagged element is activated. gtag only exists after the visitor
  // accepts analytics cookies (see includes/analytics.html); without consent this is a no-op.
  document.addEventListener('click', event => {
    const el = event.target.closest('[data-analytics]');
    if (!el || typeof window.gtag !== 'function') return;
    const params = {};
    if (el.dataset.analyticsTier) params.tier = el.dataset.analyticsTier;
    window.gtag('event', el.dataset.analytics, params);
  });
  // UX-only double-submit guard for checkout forms (pricing.html can render several, one per
  // tier): disables just the submitted button so a double-click can't fire a second POST. The
  // real protection against a duplicate Stripe Checkout Session is server-side idempotency.
  document.addEventListener('submit', event => {
    const form = event.target.closest('[data-checkout-form]'); if (!form) return;
    const button = form.querySelector('[data-checkout-submit]'); if (!button || button.disabled) return;
    button.disabled = true; button.setAttribute('aria-busy', 'true');
    button.textContent = button.dataset.loadingLabel || button.textContent;
  });
  // Show/hide password. Was an inline onclick duplicated verbatim in login.html and
  // signup.html, communicating its state only through style.opacity -- invisible to a
  // screen reader. One delegated handler, with aria-pressed carrying the state.
  document.addEventListener('click', event => {
    const button = event.target.closest('[data-password-toggle]');
    if (!button) return;
    const input = document.getElementById(button.dataset.passwordToggle);
    if (!input) return;
    const reveal = input.type === 'password';
    input.type = reveal ? 'text' : 'password';
    button.setAttribute('aria-pressed', String(reveal));
    button.setAttribute('aria-label', reveal ? 'Απόκρυψη κωδικού' : 'Εμφάνιση κωδικού');
  });

  // Mobile menu. Previously a bare class toggle: it reported no state to assistive
  // tech, and tapping an in-page anchor (#how-it-works) scrolled the page while
  // leaving the panel open on top of the target.
  const menuButton = document.getElementById('menuButton');
  const mobileMenu = document.getElementById('mobileMenu');
  if (menuButton && mobileMenu) {
    const setMenu = open => {
      mobileMenu.classList.toggle('hidden', !open);
      menuButton.setAttribute('aria-expanded', String(open));
    };
    menuButton.setAttribute('aria-expanded', 'false');
    menuButton.setAttribute('aria-controls', 'mobileMenu');
    menuButton.addEventListener('click', () => setMenu(mobileMenu.classList.contains('hidden')));
    mobileMenu.addEventListener('click', event => { if (event.target.closest('a')) setMenu(false); });
    document.addEventListener('keydown', event => {
      if (event.key !== 'Escape' || mobileMenu.classList.contains('hidden')) return;
      setMenu(false); menuButton.focus();
    });
  }
  setTimeout(() => document.querySelectorAll('[data-toast]').forEach(x => { x.style.opacity = '0'; setTimeout(() => x.remove(), 300); }), 3500);
  document.addEventListener('DOMContentLoaded', () => { reveal(); counters(); window.renderSignalChart(); sizeCompanyTable(); initKadPickers(); initReferencePickers(); });
  window.addEventListener('resize', sizeCompanyTable);
})();
