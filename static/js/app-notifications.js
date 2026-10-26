(() => {
  const colors = {
    success: { background: '#f0f7f3', color: '#24543b' },
    danger: { background: '#fff2f2', color: '#823737' },
    warning: { background: '#fff8ea', color: '#725923' },
    info: { background: '#f3f5f7', color: '#344454' },
  };
  const icons = { success: 'success', danger: 'error', error: 'error', warning: 'warning', info: 'info' };
  const notify = (message, category = 'info') => {
    if (window.Swal) return Swal.fire({
      toast: true, position: 'top-end', text: String(message),
      icon: icons[category] || 'info',
      showConfirmButton: false, showCloseButton: false,
      timer: 3500, timerProgressBar: true,
      customClass: { popup: 'app-notification-toast' },
      ...colors[category] || colors.info,
      didOpen: popup => {
        popup.addEventListener('mouseenter', Swal.stopTimer);
        popup.addEventListener('mouseleave', Swal.resumeTimer);
      },
    });
    // Keep feedback available even when the CDN cannot be reached.
    const notice = document.createElement('div');
    notice.className = 'position-fixed top-0 end-0 m-3 p-3 border rounded bg-white shadow-sm';
    notice.style.zIndex = '2000';
    notice.setAttribute('role', 'status');
    notice.textContent = String(message);
    document.body.append(notice);
    setTimeout(() => notice.remove(), 3500);
    return Promise.resolve();
  };
  window.AppAlerts = { notify };

  const confirmedForms = new WeakSet();
  document.addEventListener('submit', async event => {
    const form = event.target.closest('form[data-confirm-message]');
    if (!form || confirmedForms.has(form)) return;
    event.preventDefault();
    const submitter = event.submitter;
    if (!window.Swal) {
      notify('確認視窗未能載入，請重新整理後再試。', 'warning');
      return;
    }
    const result = await Swal.fire({
      title: '確認刪除', text: form.dataset.confirmMessage, icon: 'warning',
      showCancelButton: true, confirmButtonText: '永久刪除', cancelButtonText: '取消',
      confirmButtonColor: '#dc3545', cancelButtonColor: '#6c757d',
      focusCancel: true,
    });
    if (result.isConfirmed) {
      confirmedForms.add(form);
      form.requestSubmit(submitter);
      confirmedForms.delete(form);
    }
  });

  document.addEventListener('DOMContentLoaded', async () => {
    const messages = JSON.parse(document.getElementById('app-notifications')?.textContent || '[]');
    for (const [category, message] of messages) await notify(message, category);
  });
})();
