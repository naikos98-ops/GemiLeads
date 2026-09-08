(() => {
  const views = [...document.querySelectorAll('[data-screen]')];
  const links = [...document.querySelectorAll('[data-view]')];
  const navLinks = [...document.querySelectorAll('.rail a[data-view], .mobile-nav a[data-view]')];
  const toast = document.querySelector('.toast');
  let toastTimer;

  const showToast = message => {
    toast.textContent = message;
    toast.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toast.classList.remove('show'), 2200);
  };

  const showView = name => {
    const target = name === 'leads' ? 'dashboard' : name;
    views.forEach(view => view.classList.toggle('active', view.dataset.screen === target));
    navLinks.forEach(link => link.classList.toggle('active', link.getAttribute('href') === `#${name}` || (name === 'lead' && link.getAttribute('href') === '#lead')));
    window.scrollTo({ top: 0, behavior: 'smooth' });
    history.replaceState(null, '', `#${name}`);
  };

  links.forEach(link => link.addEventListener('click', event => {
    event.preventDefault();
    showView(link.dataset.view);
  }));
  document.querySelectorAll('[data-disabled]').forEach(el => el.addEventListener('click', event => {
    event.preventDefault(); showToast('Shown for navigation context; outside this three-screen prototype.');
  }));
  document.querySelectorAll('[data-toast]').forEach(el => el.addEventListener('click', event => {
    event.preventDefault(); showToast(el.dataset.toast);
  }));
  document.querySelectorAll('[data-open-lead]').forEach(button => button.addEventListener('click', () => showView('lead')));

  const companies = [
    {time:'11:36 · TODAY',name:'ΑΛΦΑ ΕΝΕΡΓΕΙΑΚΗ Ι.Κ.Ε.',id:'ΓΕΜΗ 182004503000 · ΑΦΜ 801234567',radar:'Energy Installers',kad:'43.21 · Electrical installation',region:'Αττική',legal:'ΙΚΕ'},
    {time:'10:54 · TODAY',name:'ΘΕΣΣΑΛΙΚΗ ΨΗΦΙΑΚΗ Μ.Ι.Κ.Ε.',id:'ΓΕΜΗ 182003121000',radar:'B2B Software',kad:'62.01 · Computer programming',region:'Λάρισα',legal:'ΜΙΚΕ'},
    {time:'09:18 · TODAY',name:'ΝΗΣΙΩΤΙΚΕΣ ΠΡΟΜΗΘΕΙΕΣ Ε.Ε.',id:'ΓΕΜΗ 182001884000',radar:'No Radar match',kad:'—',region:'Δωδεκάνησα',legal:'ΕΕ'},
    {time:'08:42 · TODAY',name:'ΠΡΑΣΙΝΗ ΔΟΜΗ Α.Ε.',id:'ΓΕΜΗ 182000612000',radar:'Construction West',kad:'41.20 · Construction',region:'Αχαΐα',legal:'ΑΕ'}
  ];
  document.querySelectorAll('.signal[data-company]').forEach(row => row.addEventListener('click', () => {
    document.querySelectorAll('.signal').forEach(x => x.classList.remove('selected')); row.classList.add('selected');
    const data = companies[Number(row.dataset.company)] || companies[0];
    Object.entries({time:'inspect-time',name:'inspect-name',id:'inspect-id',radar:'inspect-radar',kad:'inspect-kad',region:'inspect-region',legal:'inspect-legal'}).forEach(([key,id]) => document.getElementById(id).textContent = data[key]);
    const mobileSelection = document.querySelector('.mobile-selection');
    row.insertAdjacentElement('afterend', mobileSelection);
    mobileSelection.querySelector('[data-mobile-match]').textContent = data.radar === 'No Radar match' ? 'SELECTED · REGISTRATION SIGNAL' : `SELECTED · MATCHED BY ${data.radar.toUpperCase()}`;
    mobileSelection.querySelector('[data-mobile-name]').textContent = data.name;
    mobileSelection.querySelector('[data-mobile-gemi]').textContent = data.id.replace('ΓΕΜΗ ', '').split(' · ')[0];
    mobileSelection.querySelector('[data-mobile-why]').textContent = data.radar === 'No Radar match' ? 'No Radar match' : `${data.kad.split(' · ')[0]} + ${data.region}`;
  }));
  document.querySelectorAll('[data-favorite]').forEach(button => button.addEventListener('click', () => {
    const saved = button.dataset.saved !== 'true'; button.dataset.saved = String(saved); button.textContent = saved ? '★ FAVORITE' : '☆ FAVORITE'; showToast(saved ? 'Saved as favorite.' : 'Removed from favorites.');
  }));
  document.querySelectorAll('[data-toggle-radar]').forEach(button => button.addEventListener('click', () => {
    const row = button.closest('.radar-row'); const paused = !row.classList.contains('paused'); row.classList.toggle('paused', paused); row.querySelector('.radar-state').textContent = paused ? 'PAUSED' : 'ACTIVE'; row.querySelector('.radar-state').classList.toggle('on', !paused); button.textContent = paused ? 'RESUME' : 'PAUSE'; showToast(paused ? 'Radar paused.' : 'Radar resumed.');
  }));
  document.querySelector('[data-save-status]').addEventListener('click', () => { document.getElementById('last-change').textContent = '09 Sep 2026 · now'; showToast('Lead status saved in prototype state.'); });
  document.querySelector('[data-save-note]').addEventListener('click', () => showToast('Private note saved in prototype state.'));
  const initial = location.hash.slice(1); if (['dashboard','radars','lead'].includes(initial)) showView(initial);
})();
