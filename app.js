const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

function tzOffsetMinutes() {
  return new Date().getTimezoneOffset();
}

function isoLocalFromDateTime(dateStr, timeStr) {
  return `${dateStr}T${timeStr}`;
}

function fmtTimeRange(startUtcIso, endUtcIso) {
  const s = new Date(startUtcIso);
  const e = new Date(endUtcIso);
  const pad = (n) => `${n}`.padStart(2, '0');
  return `${pad(s.getHours())}:${pad(s.getMinutes())} – ${pad(e.getHours())}:${pad(e.getMinutes())}`;
}

function fmtDateLong(dateStr) {
  const d = new Date(`${dateStr}T00:00:00`);
  return d.toLocaleDateString(undefined, { weekday: 'long', year: 'numeric', month: 'short', day: 'numeric' });
}

function toast(message) {
  const el = $('#toast');
  el.textContent = message;
  el.classList.add('show');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.remove('show'), 2400);
}

async function api(url, options = {}) {
  const res = await fetch(url, options);
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.ok === false) {
    const err = data.error || `Request failed (${res.status})`;
    throw new Error(err);
  }
  return data;
}

let ME = null;
let CURRENT_DATE = null;
let APPOINTMENTS = [];

function statusPill(status) {
  const label = status.toUpperCase();
  return `<span class="status ${status}">${label}</span>`;
}

function escapeHtml(s) {
  return (s ?? '').toString()
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

function renderSchedule() {
  const container = $('#schedule');
  const day = CURRENT_DATE;
  const items = APPOINTMENTS
    .filter(a => new Date(a.start_at_utc).toLocaleDateString('en-CA') === day)
    .sort((a, b) => new Date(a.start_at_utc) - new Date(b.start_at_utc));

  $('#scheduleSub').textContent = `${fmtDateLong(day)} • ${items.length} appointment(s)`;

  if (!items.length) {
    container.innerHTML = `<div class="card"><div class="muted">No appointments for this day.</div></div>`;
    return;
  }

  container.innerHTML = items.map(a => {
    const subtitle = [
      a.reason ? escapeHtml(a.reason) : '—',
      a.patient_phone ? `📞 ${escapeHtml(a.patient_phone)}` : null,
    ].filter(Boolean).join(' • ');
    const actions = [];
    actions.push(`<button class="btn" data-act="details" data-id="${a.id}">Details</button>`);
    if (ME.role === 'receptionist' && a.status === 'booked') {
      actions.push(`<button class="btn" data-act="remind" data-id="${a.id}">Send reminder</button>`);
      actions.push(`<button class="btn" data-act="reschedule" data-id="${a.id}">Reschedule</button>`);
      actions.push(`<button class="btn danger" data-act="cancel" data-id="${a.id}">Cancel</button>`);
    }
    if (ME.role === 'doctor' && a.status === 'booked') {
      actions.push(`<button class="btn" data-act="complete" data-id="${a.id}">Mark completed</button>`);
    }
    return `
      <div class="slot">
        <div class="slot-left">
          <div class="time">${fmtTimeRange(a.start_at_utc, a.end_at_utc)}</div>
          <div>
            <div class="slot-title">${escapeHtml(a.patient_name)}</div>
            <div class="slot-sub">${subtitle}</div>
          </div>
        </div>
        <div class="slot-actions">
          ${statusPill(a.status)}
          ${actions.join('')}
        </div>
      </div>
    `;
  }).join('');
}

function renderUpcoming() {
  const container = $('#upcoming');
  const items = APPOINTMENTS
    .filter(a => a.status === 'booked')
    .sort((a, b) => new Date(a.start_at_utc) - new Date(b.start_at_utc))
    .slice(0, 8);

  if (!items.length) {
    container.innerHTML = `<div class="muted">No upcoming booked appointments.</div>`;
    return;
  }

  container.innerHTML = items.map(a => `
    <div class="row">
      <div class="row-main">
        <div class="row-title">${escapeHtml(a.patient_name)} <span class="pill">${new Date(a.start_at_utc).toLocaleDateString()}</span></div>
        <div class="row-sub">${fmtTimeRange(a.start_at_utc, a.end_at_utc)} • ${escapeHtml(a.reason || '—')}</div>
      </div>
      <div class="row-actions">
        <button class="btn" data-act="details" data-id="${a.id}">Details</button>
        <button class="btn" data-act="reschedule" data-id="${a.id}">Reschedule</button>
        <button class="btn danger" data-act="cancel" data-id="${a.id}">Cancel</button>
      </div>
    </div>
  `).join('');
}

function renderStats(stats) {
  $('#statsCards').innerHTML = `
    <div class="stat"><div class="stat-k">Booked</div><div class="stat-v">${stats.booked ?? 0}</div></div>
    <div class="stat"><div class="stat-k">Completed</div><div class="stat-v">${stats.completed ?? 0}</div></div>
    <div class="stat"><div class="stat-k">Canceled</div><div class="stat-v">${stats.canceled ?? 0}</div></div>
    <div class="stat"><div class="stat-k">Total</div><div class="stat-v">${stats.total ?? 0}</div></div>
  `;
}

function showView(name) {
  $$('.nav-item').forEach(b => b.classList.toggle('active', b.dataset.view === name));
  $$('.view').forEach(v => v.classList.add('hidden'));
  $(`#view-${name}`).classList.remove('hidden');
}

function openModal(title, subtitle, bodyHtml) {
  $('#modalTitle').textContent = title;
  $('#modalSub').textContent = subtitle;
  $('#modalBody').innerHTML = bodyHtml;
  $('#modal').classList.remove('hidden');
}

function closeModal() {
  $('#modal').classList.add('hidden');
  $('#modalBody').innerHTML = '';
}

function findAppt(id) {
  return APPOINTMENTS.find(a => a.id === id);
}

async function refresh() {
  const date = CURRENT_DATE;
  const headers = { 'X-TZ-Offset': `${tzOffsetMinutes()}` };
  const data = await api(`/api/appointments?date=${encodeURIComponent(date)}`, { headers });
  APPOINTMENTS = data.appointments || [];
  renderSchedule();
  if (ME.role === 'receptionist') renderUpcoming();
}

async function refreshStats() {
  const date = CURRENT_DATE;
  const headers = { 'X-TZ-Offset': `${tzOffsetMinutes()}` };
  const stats = await api(`/api/stats?date=${encodeURIComponent(date)}`, { headers });
  renderStats(stats.stats || {});
}

async function init() {
  const me = await api('/api/me');
  ME = me.user;
  $('#uName').textContent = ME.username;
  $('#uRole').textContent = ME.role === 'receptionist' ? 'Receptionist' : 'Doctor';

  if (ME.role !== 'receptionist') {
    $('#bookTab').style.display = 'none';
  }

  const demoTools = $('#demoTools');
  const resetDemoBtn = $('#resetDemoBtn');
  if (demoTools && resetDemoBtn && ME.role === 'doctor') {
    demoTools.style.display = 'block';
    resetDemoBtn.addEventListener('click', async () => {
      if (!confirm('Reset all appointments to demo data for the selected day?')) return;
      await api('/api/demo/reset', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ date_local: CURRENT_DATE, tz_offset_minutes: tzOffsetMinutes() }),
      });
      toast('Demo data reset');
      await refresh();
      await refreshStats();
    });
  }

  const dayInput = $('#day');
  const today = new Date().toLocaleDateString('en-CA');
  CURRENT_DATE = today;
  dayInput.value = today;
  dayInput.addEventListener('change', async () => {
    CURRENT_DATE = dayInput.value;
    await refresh();
    await refreshStats();
  });

  $$('.nav-item').forEach(btn => btn.addEventListener('click', () => showView(btn.dataset.view)));

  $('#logoutBtn').addEventListener('click', async () => {
    await api('/api/auth/logout', { method: 'POST' });
    location.href = '/login';
  });
  $('#printBtn').addEventListener('click', () => window.print());

  $('#modalClose').addEventListener('click', closeModal);
  $('#modal').addEventListener('click', (e) => { if (e.target.id === 'modal') closeModal(); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeModal(); });

  const bookForm = $('#bookForm');
  if (bookForm) {
    const dateField = bookForm.querySelector('input[name="date"]');
    dateField.value = CURRENT_DATE;
    const timeField = bookForm.querySelector('input[name="time"]');
    timeField.value = new Date().toTimeString().slice(0, 5);

    const availMsg = $('#availMsg');
    async function checkAvail() {
      availMsg.textContent = '';
      const date = dateField.value;
      const time = timeField.value;
      const duration = parseInt(bookForm.querySelector('select[name="duration"]').value, 10);
      const start_local = isoLocalFromDateTime(date, time);
      const payload = { start_local, duration_minutes: duration, tz_offset_minutes: tzOffsetMinutes() };
      const data = await api('/api/availability', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
      if (data.available) {
        availMsg.textContent = 'Available ✅';
        availMsg.style.color = 'var(--ok)';
      } else {
        availMsg.textContent = 'Not available (clash detected) ❌';
        availMsg.style.color = 'var(--danger)';
      }
    }

    $('#availBtn').addEventListener('click', async () => {
      try { await checkAvail(); } catch (e) { toast(e.message); }
    });

    bookForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      availMsg.textContent = '';
      const fd = new FormData(bookForm);
      const payload = {
        patient_name: fd.get('patient_name')?.toString() ?? '',
        patient_phone: fd.get('patient_phone')?.toString() ?? '',
        reason: fd.get('reason')?.toString() ?? '',
        notes: fd.get('notes')?.toString() ?? '',
        start_local: isoLocalFromDateTime(fd.get('date')?.toString() ?? '', fd.get('time')?.toString() ?? ''),
        duration_minutes: parseInt(fd.get('duration')?.toString() ?? '15', 10),
        tz_offset_minutes: tzOffsetMinutes(),
      };
      try {
        await api('/api/appointments', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
        toast('Booked ✅');
        bookForm.reset();
        dateField.value = CURRENT_DATE;
        timeField.value = new Date().toTimeString().slice(0, 5);
        await refresh();
        await refreshStats();
      } catch (err) {
        toast(err.message);
      }
    });
  }

  document.body.addEventListener('click', async (e) => {
    const t = e.target;
    if (!(t instanceof HTMLElement)) return;
    const act = t.dataset.act;
    if (!act) return;
    const id = parseInt(t.dataset.id, 10);
    const a = findAppt(id);
    if (!a) return;

    try {
      if (act === 'details') {
        const body = `
          <div class="card" style="padding:12px">
            <div class="row-title">${escapeHtml(a.patient_name)}</div>
            <div class="row-sub">${fmtTimeRange(a.start_at_utc, a.end_at_utc)} • ${escapeHtml(a.reason || '—')}</div>
            <div style="margin-top:10px; display:flex; gap:10px; flex-wrap:wrap">
              ${a.patient_phone ? `<span class="pill">Phone: ${escapeHtml(a.patient_phone)}</span>` : `<span class="pill">No phone</span>`}
              <span class="pill">Status: ${escapeHtml(a.status)}</span>
              ${a.reminder_sent_at ? `<span class="pill">Reminder sent</span>` : ``}
            </div>
            ${a.notes ? `<div style="margin-top:10px" class="muted mini">Notes: ${escapeHtml(a.notes)}</div>` : ``}
          </div>
        `;
        openModal('Appointment details', new Date(a.start_at_utc).toLocaleString(), body);
      }

      if (act === 'cancel') {
        if (!confirm('Cancel this appointment?')) return;
        await api(`/api/appointments/${id}/cancel`, { method: 'POST' });
        toast('Canceled');
        await refresh();
        await refreshStats();
      }

      if (act === 'complete') {
        await api(`/api/appointments/${id}/complete`, { method: 'POST' });
        toast('Completed');
        await refresh();
        await refreshStats();
      }

      if (act === 'remind') {
        await api(`/api/appointments/${id}/remind`, { method: 'POST' });
        toast('Reminder queued (demo)');
        await refresh();
      }

      if (act === 'reschedule') {
        const s = new Date(a.start_at_utc);
        const localDate = s.toLocaleDateString('en-CA');
        const localTime = s.toTimeString().slice(0, 5);
        const duration = Math.max(5, Math.round((new Date(a.end_at_utc) - new Date(a.start_at_utc)) / 60000));
        openModal('Reschedule', a.patient_name, `
          <form id="resForm" class="form">
            <div class="row3">
              <label class="field"><span>Date</span><input name="date" type="date" value="${localDate}" required /></label>
              <label class="field"><span>Time</span><input name="time" type="time" value="${localTime}" required /></label>
              <label class="field"><span>Duration</span>
                <select name="duration">
                  ${[10,15,20,30,45,60].map(m => `<option value="${m}" ${m===duration?'selected':''}>${m} min</option>`).join('')}
                </select>
              </label>
            </div>
            <div class="actions">
              <button class="btn" type="button" id="resAvail">Check availability</button>
              <button class="btn primary" type="submit">Confirm</button>
            </div>
            <div class="muted mini" id="resMsg"></div>
          </form>
        `);
        const form = $('#resForm');
        const resMsg = $('#resMsg');
        async function checkResAvail() {
          resMsg.textContent = '';
          const fd = new FormData(form);
          const start_local = isoLocalFromDateTime(fd.get('date'), fd.get('time'));
          const payload = { start_local, duration_minutes: parseInt(fd.get('duration'), 10), tz_offset_minutes: tzOffsetMinutes(), exclude_appointment_id: id };
          const data = await api('/api/availability', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
          resMsg.textContent = data.available ? 'Available ✅' : 'Not available (clash) ❌';
          resMsg.style.color = data.available ? 'var(--ok)' : 'var(--danger)';
        }
        $('#resAvail').addEventListener('click', async () => { try { await checkResAvail(); } catch (e) { toast(e.message); }});
        form.addEventListener('submit', async (ev) => {
          ev.preventDefault();
          const fd = new FormData(form);
          const payload = {
            start_local: isoLocalFromDateTime(fd.get('date'), fd.get('time')),
            duration_minutes: parseInt(fd.get('duration'), 10),
            tz_offset_minutes: tzOffsetMinutes(),
          };
          await api(`/api/appointments/${id}/reschedule`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
          toast('Rescheduled ✅');
          closeModal();
          await refresh();
        });
      }
    } catch (err) {
      toast(err.message);
    }
  });

  await refresh();
  await refreshStats();
}

init().catch(() => {
  location.href = '/login';
});
