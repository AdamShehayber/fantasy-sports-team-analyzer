// Week 4: Live search & add with debounce and non-blocking toast

(function(){
  const qs = (sel) => document.querySelector(sel);
  const qsa = (sel) => Array.from(document.querySelectorAll(sel));

  const toastEl = qs('#toast');
  function showToast(msg){
    if(!toastEl) return;
    toastEl.textContent = msg;
    toastEl.style.display = 'block';
    setTimeout(()=>{ toastEl.style.display='none'; }, 3000);
  }

  // Themed modal dialog for errors/warnings/info
  const modalEl = qs('#modal');
  const backdropEl = qs('#modal-backdrop');
  const modalTitle = qs('#modal-title');
  const modalBody = qs('#modal-body');
  const modalCloseBtn = qs('#modal-close');
  function showDialog(title, message, type='info'){
    if(!modalEl || !backdropEl){ showToast(message); return; }
    modalEl.classList.remove('modal-error','modal-success','modal-warning','modal-info');
    modalEl.classList.add('modal-' + (type || 'info'));
    modalTitle && (modalTitle.textContent = title || 'Notice');
    modalBody && (modalBody.textContent = message || '');
    backdropEl.style.display = 'block';
    modalEl.style.display = 'block';
    document.body.classList.add('modal-open');
  }
  function closeDialog(){
    if(backdropEl) backdropEl.style.display = 'none';
    if(modalEl) modalEl.style.display = 'none';
    document.body.classList.remove('modal-open');
  }
  if(modalCloseBtn) modalCloseBtn.addEventListener('click', closeDialog);
  if(backdropEl) backdropEl.addEventListener('click', closeDialog);
  document.addEventListener('keydown', (e)=>{ if(e.key === 'Escape') closeDialog(); });

  function debounce(fn, ms){
    let t;
    return function(...args){
      clearTimeout(t);
      t = setTimeout(()=>fn.apply(this,args), ms);
    };
  }

  const input = qs('#search-input');
  const posSel = qs('#search-position');
  const teamInput = qs('#search-team');
  const statusEl = qs('#search-status');
  const tbody = qs('#search-results-body');
  const csrfToken = (qs('#global-csrf')||{}).value || '';
  let searchController = null;
  const searchBtn = qs('#search-button');

  async function runSearch(){
    const q = (input?.value||'').trim();
    const team = (teamInput?.value||'').trim();
    const pos = (posSel?.value||'').trim();
    if(!q || q.length < 3){
      statusEl && (statusEl.textContent = 'Type 3+ letters to search players');
      if(tbody) tbody.innerHTML='';
      return;
    }
    statusEl && (statusEl.textContent = 'Searching…');
    try{
      const url = `/api/search?q=${encodeURIComponent(q)}&team=${encodeURIComponent(team)}&position=${encodeURIComponent(pos)}`;
      // Abort any in-flight search to reduce noisy aborted requests in console
      if (searchController) { try { searchController.abort(); } catch(_) {} }
      searchController = new AbortController();
      const resp = await fetch(url, { method:'GET', signal: searchController.signal });
      if(!resp.ok){ throw new Error('Search failed'); }
      const json = await resp.json();
      const results = json?.results || [];
      renderResults(results);
      statusEl && (statusEl.textContent = `${results.length} result(s)`);
    }catch(err){
      // Ignore intentional aborts due to rapid typing
      if (err && err.name === 'AbortError') { return; }
      renderResults([]);
      statusEl && (statusEl.textContent = 'Live data unavailable. Try again.');
      showToast('Live data unavailable. Try again.');
    }
  }

  // Manual search only: run when Search button is clicked
  if (searchBtn){
    searchBtn.addEventListener('click', ()=>{
      runSearch();
    });
  }

  function renderResults(list){
    if(!tbody) return;
    tbody.innerHTML = '';
    list.forEach(item=>{
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td class="ta-left">${escapeHtml(item.full_name||'')}</td>
        <td class="ta-center">${escapeHtml(item.position||'')}</td>
        <td class="ta-center">${escapeHtml(item.team||'')}</td>
        <td class="ta-right">${fmt(item.projection_points)} <span class="proj-badge live">Live</span></td>
        <td class="ta-center">
          <button class="btn-small" data-action="add" data-target="starter">Add Starter</button>
          <button class="btn-small" data-action="add" data-target="bench">Add Bench</button>
          <button class="btn-small" data-action="watch">Watchlist</button>
        </td>
      `;
      // attach handlers
      tr.querySelectorAll('button[data-action="add"]').forEach(btn=>{
        btn.addEventListener('click', async ()=>{
          const target = btn.getAttribute('data-target');
          try{
            const fd = new FormData();
            fd.append('csrf_token', csrfToken);
            fd.append('player_id', item.player_id||'');
            fd.append('full_name', item.full_name||'');
            fd.append('position', item.position||'');
            fd.append('team', item.team||'');
            fd.append('target', target);
            const resp = await fetch('/players/add_from_search', { method:'POST', body: fd, redirect:'follow' });
            if (resp.redirected) {
              // Follow server redirect URL (usually /dashboard)
              window.location.assign(resp.url);
              return;
            }
            if(resp.ok){
              window.location.assign('/dashboard');
            } else {
              showToast('Unable to add player.');
            }
          }catch(err){
            showToast('Unable to add player.');
          }
        });
      });
      const watchBtn = tr.querySelector('button[data-action="watch"]');
      if (watchBtn){
        watchBtn.addEventListener('click', async ()=>{
          try{
            const fd = new FormData();
            fd.append('csrf_token', csrfToken);
            fd.append('name', item.full_name||'');
            fd.append('position', item.position||'');
            fd.append('team', item.team||'');
            fd.append('player_id', item.player_id||'');
            const resp = await fetch('/watchlist/add', { method:'POST', body: fd, redirect:'follow' });
            if(resp.ok){
              showToast('Added to watchlist');
              appendToWatchlist(item.full_name||'', item.position||'', item.team||'');
            }
            else { showToast('Could not add to watchlist'); }
          }catch(err){ showToast('Could not add to watchlist'); }
        });
      }
      tbody.appendChild(tr);
    });
  }

  function escapeHtml(str){
    return String(str).replace(/[&<>"']/g, s=>({
      '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;','\'':'&#39;'
    })[s]);
  }
  function fmt(n){
    const v = parseFloat(n||0);
    return v.toFixed(2);
  }

  // Confirm Clear All Players
  const clearForm = qs('#clear-all-form');
  if(clearForm){
    clearForm.addEventListener('submit', (e)=>{
      const ok = window.confirm('Are you sure you want to clear all players?');
      if(!ok){ e.preventDefault(); }
    });
  }

  // Intercept Watchlist Add form to update list immediately
  const wlForm = qs('#watchlist-form');
  if(wlForm){
    wlForm.addEventListener('submit', async (e)=>{
      e.preventDefault();
      try{
        const fd = new FormData(wlForm);
        const resp = await fetch(wlForm.action, { method:'POST', body: fd });
        if(resp.ok){
          const name = (fd.get('name')||'').toString();
          appendToWatchlist(name, '', '');
          showToast('Added to watchlist');
          wlForm.reset();
        } else {
          showToast('Could not add to watchlist');
        }
      }catch(err){ showToast('Could not add to watchlist'); }
    });
  }

  // Intercept Refresh button submit to prevent stuck buffering and navigate on redirect
  const refreshForm = document.querySelector('form[action="/stats/sync"]');
  if(refreshForm){
    const btn = refreshForm.querySelector('button[type="submit"]');
    refreshForm.addEventListener('submit', async (e)=>{
      e.preventDefault();
      if(btn){ btn.disabled = true; btn.textContent = 'Refreshing…'; }
      try{
        const fd = new FormData(refreshForm);
        const resp = await fetch(refreshForm.action, { method:'POST', body: fd, redirect: 'follow' });
        // Consume response body and refresh regardless of redirect handling differences in WebView
        try { await resp.text(); } catch(_) {}
        const targetUrl = resp?.url || window.location.href;
        // Prefer navigation to ensure flash messages render; fallback to reload
        setTimeout(()=>{
          try { window.location.replace(targetUrl); } catch(_) { window.location.reload(); }
        }, 100);
      } catch(err){
        showDialog('Refresh Failed', 'Live data could not be refreshed. Please try again.', 'error');
        if(btn){ btn.disabled = false; btn.textContent = 'Refresh Now'; }
      }
    });
    // Keyboard refresh shortcuts for desktop app (F5 / Ctrl+R)
    window.addEventListener('keydown', (ev)=>{
      const isRefresh = (ev.key === 'F5') || (ev.ctrlKey && (ev.key.toLowerCase() === 'r'));
      if(isRefresh){ ev.preventDefault(); try { window.location.reload(); } catch(_) {} }
    });
  }

  // Helper: update Watchlist UI immediately
  function appendToWatchlist(name, position, team){
    const list = qs('#watchlist-items');
    if(!list) return;
    // Remove empty-state if present
    const empty = Array.from(list.children).find(li=> (li.textContent||'').trim().toLowerCase() === 'no players yet.');
    if(empty) empty.remove();
    // Prevent duplicates (simple text match)
    const itemText = `${name}${position?` (${position})`:''}${team?` - ${team}`:''}`;
    const exists = Array.from(list.children).some(li=> (li.textContent||'').trim() === itemText.trim());
    if(exists) return;
    const li = document.createElement('li');
    li.textContent = itemText;
    list.appendChild(li);
  }

  // Plotly charts
  function renderCharts(){
    const breakdown = window.__breakdown || {};
    const hist = window.__strength_history || [];
    const starter = parseFloat(window.__starter_strength||0);
    const bench = parseFloat(window.__bench_strength||0);

    // Pie chart
    const labels = Object.keys(breakdown||{});
    const values = labels.map(k=> (breakdown[k]?.starter||0) + (breakdown[k]?.bench||0));
    const pieEl = qs('#chart-pie');
    if(pieEl && labels.length){
      Plotly.newPlot(pieEl, [{ type:'pie', labels, values, hole:.3 }], { template:'plotly_dark', margin:{t:20,l:10,r:10,b:10} }, {displayModeBar:false});
      const exportPie = qs('#export-pie');
      exportPie && exportPie.addEventListener('click', async ()=>{
        const url = await Plotly.toImage(pieEl, {format:'png', width:800, height:480, scale:1});
        downloadDataUrl(url, 'position_breakdown.png');
      });
    }

    // Line chart
    const weeks = hist.map(h=> h.week);
    const sVals = hist.map(h=> h.starter_total);
    const bVals = hist.map(h=> h.bench_total);
    const lineEl = qs('#chart-line');
    if(lineEl && weeks.length){
      Plotly.newPlot(lineEl, [
        { x: weeks, y: sVals, type:'scatter', mode:'lines+markers', name:'Starters' },
        { x: weeks, y: bVals, type:'scatter', mode:'lines+markers', name:'Bench' }
      ], { template:'plotly_dark', margin:{t:20,l:30,r:10,b:30}, xaxis:{title:'Week'}, yaxis:{title:'Strength'} }, {displayModeBar:false});
      const exportLine = qs('#export-line');
      exportLine && exportLine.addEventListener('click', async ()=>{
        const url = await Plotly.toImage(lineEl, {format:'png', width:800, height:480, scale:1});
        downloadDataUrl(url, 'team_strength_over_weeks.png');
      });
    }

    // Bar chart
    const barEl = qs('#chart-bar');
    if(barEl){
      Plotly.newPlot(barEl, [{ x:['Starters','Bench'], y:[starter, bench], type:'bar' }], { template:'plotly_dark', margin:{t:20,l:30,r:10,b:30} }, {displayModeBar:false});
      const exportBar = qs('#export-bar');
      exportBar && exportBar.addEventListener('click', async ()=>{
        const url = await Plotly.toImage(barEl, {format:'png', width:600, height:380, scale:1});
        downloadDataUrl(url, 'starters_vs_bench.png');
      });
    }
  }

  function downloadDataUrl(dataUrl, filename){
    const a = document.createElement('a');
    a.href = dataUrl;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  // Programmatic downloads to avoid net::ERR_ABORTED in preview when using form GET
  function filenameFromDisposition(resp){
    const cd = resp.headers.get('Content-Disposition') || '';
    const m = cd.match(/filename\s*=\s*"?([^";]+)"?/i);
    return m ? m[1] : '';
  }
  async function fetchAndDownload(url, fallbackName){
    const resp = await fetch(url, { method:'GET' });
    if(!resp.ok) throw new Error('Download failed');
    const blob = await resp.blob();
    const name = filenameFromDisposition(resp) || fallbackName || 'download';
    const objectUrl = URL.createObjectURL(blob);
    downloadDataUrl(objectUrl, name);
    setTimeout(()=>URL.revokeObjectURL(objectUrl), 0);
  }

  // Intercept Reports page forms and perform fetch-based download
  function interceptDownloadForm(form){
    if(!form) return;
    form.addEventListener('submit', async (e)=>{
      e.preventDefault();
      try{
        const params = new URLSearchParams();
        const fd = new FormData(form);
        for (const [k,v] of fd.entries()) params.set(k, v);
        const url = form.action + (params.toString() ? ('?' + params.toString()) : '');
        let fallbackName = 'download';
        const fmt = params.get('format');
        if (fmt === 'csv') fallbackName = 'team_report.csv';
        if (fmt === 'pdf') fallbackName = 'team_report.pdf';
        if (form.action.endsWith('/export/csv/roster')) fallbackName = 'roster.csv';
        await fetchAndDownload(url, fallbackName);
      }catch(err){
        showDialog('Download Failed', 'Could not export report. Please try again.', 'error');
      }
    });
  }

  qsa('form[action="/reports/download"]').forEach(interceptDownloadForm);
  interceptDownloadForm(qs('form[action="/export/csv/roster"]'));

  // Initialize charts if Plotly is available
  if (typeof Plotly !== 'undefined'){
    try { renderCharts(); } catch(e) { /* ignore */ }
  }

  // Trade page: Custom multi-select dropdowns
  function initCustomMultiSelects(){
    const form = document.getElementById('trade-form');
    if(!form) return;

    const giveDropdown = document.getElementById('give-multiselect');
    const receiveDropdown = document.getElementById('receive-multiselect');

    function setupDropdown(dropdownEl) {
      if (!dropdownEl) return;

      const hiddenInput = dropdownEl.querySelector('.multiselect-hidden-input');
      const display = dropdownEl.querySelector('.multiselect-display');
      const menu = dropdownEl.querySelector('.multiselect-menu');
      const searchInput = dropdownEl.querySelector('.multiselect-search');
      const optionsContainer = dropdownEl.querySelector('.multiselect-options');
      const loadingIndicator = dropdownEl.querySelector('.multiselect-loading');

      let options = [];
      let selectedValues = [];

      function toggleMenu(forceOpen = null) {
        const isOpen = dropdownEl.classList.contains('open');
        if (forceOpen === true || (forceOpen === null && !isOpen)) {
          dropdownEl.classList.add('open');
          searchInput.focus();
        } else {
          dropdownEl.classList.remove('open');
        }
      }

      function renderOptions() {
        const query = searchInput.value.toLowerCase();
        optionsContainer.innerHTML = '';
        const filteredOptions = options.filter(opt => 
          opt.name.toLowerCase().includes(query) || 
          opt.position.toLowerCase().includes(query) || 
          opt.team.toLowerCase().includes(query)
        );

        if (filteredOptions.length === 0) {
          optionsContainer.innerHTML = '<div class="multiselect-option-placeholder">No players match search.</div>';
        }

        filteredOptions.forEach(opt => {
          const isSelected = selectedValues.includes(opt.name);
          const optEl = document.createElement('div');
          optEl.className = `multiselect-option ${isSelected ? 'selected' : ''}`;
          optEl.dataset.value = opt.name;
          optEl.innerHTML = `
            <input type="checkbox" ${isSelected ? 'checked' : ''} tabindex="-1">
            <span>${opt.name} <small>(${opt.position} - ${opt.team})</small></span>
          `;
          optEl.addEventListener('click', () => toggleSelection(opt.name));
          optionsContainer.appendChild(optEl);
        });
      }

      function toggleSelection(value) {
        if (selectedValues.includes(value)) {
          selectedValues = selectedValues.filter(v => v !== value);
        } else {
          selectedValues.push(value);
        }
        syncState();
      }

      function syncState() {
        hiddenInput.value = selectedValues.join(', ');
        renderSelectedLabels();
        renderOptions();
      }

      function renderSelectedLabels() {
        display.innerHTML = '';
        selectedValues.forEach(val => {
          const labelEl = document.createElement('span');
          labelEl.className = 'multiselect-selected-label';
          labelEl.textContent = val;
          const unselectBtn = document.createElement('span');
          unselectBtn.className = 'multiselect-unselect';
          unselectBtn.innerHTML = '&times;';
          unselectBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            toggleSelection(val);
          });
          labelEl.appendChild(unselectBtn);
          display.appendChild(labelEl);
        });
        if (selectedValues.length === 0) {
          const placeholder = document.createElement('span');
          placeholder.className = 'multiselect-placeholder';
          placeholder.textContent = display.dataset.placeholder || 'Select players...';
          display.appendChild(placeholder);
        }
      }

      async function loadPlayers(url) {
        if (loadingIndicator) { loadingIndicator.style.display = 'block'; }
        try {
          const resp = await fetch(url, { headers: { 'Accept': 'application/json' } });
          const data = await resp.json();
          options = data.players || [];
          selectedValues = []; // Reset selection on new data
          syncState();
        } catch (err) {
          options = [];
          console.error('Failed to load players:', err);
        } finally {
          if (loadingIndicator) { loadingIndicator.style.display = 'none'; }
          renderOptions();
        }
      }

      function setOptions(newOptions){
        options = Array.isArray(newOptions) ? newOptions : [];
        selectedValues = [];
        syncState();
      }

      display.addEventListener('click', () => toggleMenu());
      searchInput.addEventListener('input', renderOptions);
      document.addEventListener('click', (e) => {
        if (!dropdownEl.contains(e.target)) {
          toggleMenu(false);
        }
      });

      // Expose methods
      dropdownEl.load = loadPlayers;
      dropdownEl.setOptions = setOptions;
      // Initial render of placeholder
      renderSelectedLabels();
    }

    setupDropdown(giveDropdown);
    setupDropdown(receiveDropdown);

    // Initial load for 'Give' dropdown
    if (giveDropdown) {
      giveDropdown.load('/my/players/json');
    }

    // Expose a global function to load the 'Receive' dropdown
    window.loadReceiveDropdown = (rosterId) => {
      if (receiveDropdown) {
        if (rosterId) {
          receiveDropdown.load(`/rosters/${rosterId}/players_json`);
        } else {
          receiveDropdown.setOptions([]);
        }
      }
    };
    // Backward-compat alias
    window.loadReceivePicker = window.loadReceiveDropdown;
  }
  document.addEventListener('DOMContentLoaded', initCustomMultiSelects);

  // Custom dropdown for Against Roster
  (function initRosterDropdown(){
    const dd = document.getElementById('other-roster-dropdown');
    if(!dd) return;
    const toggle = dd.querySelector('#other-roster-toggle');
    const menu = dd.querySelector('#other-roster-menu');
    const labelEl = dd.querySelector('#other-roster-label');
    const hiddenInput = dd.querySelector('#other-roster-id');
    const items = Array.from(dd.querySelectorAll('.dropdown-item'));

    function open(){ dd.classList.add('open'); setTimeout(()=>{
      const firstItem = items[0]; firstItem && firstItem.focus();
    }, 0); }
    function close(){ dd.classList.remove('open'); }
    function setSelected(val, label){
      hiddenInput.value = val || '';
      labelEl.textContent = label || 'Select roster…';
      items.forEach(i=> i.classList.toggle('selected', i.getAttribute('data-value') === val));
      if (window.loadReceiveDropdown) {
        try { window.loadReceiveDropdown(val); } catch(_) {}
      }
    }

    // Initialize default selection to first item
    if(items.length){
      const first = items[0]; setSelected(first.getAttribute('data-value'), first.getAttribute('data-label'));
    }

    toggle.addEventListener('click', ()=>{
      const isOpen = dd.classList.contains('open');
      if(isOpen) close(); else open();
    });
    items.forEach(i=>{
      i.addEventListener('click', ()=>{
        setSelected(i.getAttribute('data-value'), i.getAttribute('data-label'));
        close();
      });
      i.addEventListener('keydown', (ev)=>{
        const idx = items.indexOf(i);
        if(ev.key === 'ArrowDown'){ ev.preventDefault(); const n = items[Math.min(items.length-1, idx+1)]; n && n.focus(); }
        if(ev.key === 'ArrowUp'){ ev.preventDefault(); const p = items[Math.max(0, idx-1)]; p && p.focus(); }
        if(ev.key === 'Enter'){ ev.preventDefault(); i.click(); }
        if(ev.key === 'Escape'){ ev.preventDefault(); close(); toggle.focus(); }
      });
    });

    // Close on outside click
    document.addEventListener('click', (ev)=>{
      if(!dd.classList.contains('open')) return;
      if(!dd.contains(ev.target)) close();
    });
  })();
})();
