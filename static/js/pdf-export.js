(() => {
  const modalElement = document.getElementById('pdf-export-modal');
  const form = document.getElementById('pdf-export-form');
  if (!modalElement || !form) return;

  document.addEventListener('click', event => {
    const link = event.target.closest('a[href]');
    if (!link || !new URL(link.href).pathname.endsWith('/report.pdf')) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    const params = new URLSearchParams(location.search);
    // The standalone angle screen uses the same options with shorter names.
    const angleForm = document.querySelector('.angle-settings-form');
    if (angleForm) {
      params.set('angle_settings', '1');
      const names = { automatic_range: 'angle_automatic_range', start_a: 'angle_start_a', start_b: 'angle_start_b',
        end_a: 'angle_end_a', end_b: 'angle_end_b', auto_scale: 'angle_auto_scale', scale: 'angle_scale',
        auto_offset: 'angle_auto_offset', offset_s: 'angle_offset_s' };
      for (const [name, target] of Object.entries(names)) {
        const field = angleForm.elements.namedItem(name);
        params.delete(target);
        if (field && (field.type !== 'checkbox' || field.checked)) params.set(target, field.type === 'checkbox' ? 'on' : field.value);
      }
    }
    const fields = document.getElementById('pdf-export-settings');
    fields.replaceChildren();
    for (const [name, value] of params) {
      if (name !== 'ftp_w' && !name.startsWith('angle_')) continue;
      if (name === 'angle_section') continue;
      const input = document.createElement('input');
      input.type = 'hidden'; input.name = name; input.value = value;
      fields.append(input);
    }
    bootstrap.Modal.getOrCreateInstance(modalElement).show();
  }, true);

  modalElement.querySelectorAll('[data-report-check]').forEach(button => {
    button.addEventListener('click', () => form.querySelectorAll('[name="section"]').forEach(box => {
      box.checked = button.dataset.reportCheck === 'all';
    }));
  });
  let exporting = false;
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (exporting) return;
    if (!form.querySelector('[name="section"]:checked')) {
      window.AppAlerts.notify('請至少選擇一個分析項目。', 'warning');
      return;
    }
    const button = form.querySelector('[type="submit"]');
    const status = document.getElementById('pdf-export-status');
    exporting = true;
    button.disabled = true;
    button.textContent = '正在產生 PDF…';
    status.hidden = false;
    status.classList.remove('text-danger');
    status.textContent = '正在整理圖表與分頁，請稍候。';
    try {
      const data = new FormData(form);
      data.set('report_phase', sessionStorage.getItem(`angle-phase:${location.pathname}`) || '0');
      const response = await fetch(form.action, { method: 'POST', body: data });
      if (!response.ok) {
        const error = await response.json().catch(() => ({}));
        throw new Error(error.error || 'PDF 下載失敗，請稍後重試。');
      }
      if (!response.headers.get('Content-Type')?.includes('application/pdf')) throw new Error('伺服器未傳回 PDF，請重新整理後重試。');
      const blob = await response.blob();
      const objectUrl = URL.createObjectURL(blob);
      const filename = response.headers.get('Content-Disposition')?.match(/filename="?([^";]+)"?/)?.[1] || 'analysis.pdf';
      const link = document.createElement('a');
      link.href = objectUrl; link.download = filename;
      document.body.append(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(objectUrl), 60000);
      status.hidden = true;
      window.AppAlerts.notify('PDF 已產生，下載已開始。', 'success');
    } catch (error) {
      status.hidden = true;
      window.AppAlerts.notify(error.message, 'danger');
    } finally {
      exporting = false;
      button.disabled = false;
      button.textContent = '下載 PDF';
    }
  });
})();
