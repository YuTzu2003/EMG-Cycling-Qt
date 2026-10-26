const readAnalysisData = (id) => {
  const element = document.getElementById(id);
  return element ? JSON.parse(element.textContent) : null;
};

document.addEventListener('click', (event) => {
  const pdfLink = event.target.closest('[data-angle-pdf]');
  if (pdfLink) {
    const form = document.querySelector('.angle-settings-form');
    if (form) {
      const params = new URLSearchParams({ angle_export: '1' });
      for (const name of ['automatic_range', 'start_a', 'start_b', 'end_a', 'end_b', 'bins', 'percentile', 'auto_scale', 'scale', 'auto_offset', 'offset_s']) {
        const field = form.elements.namedItem(name);
        if (!field || field.disabled) continue;
        if (field.type !== 'checkbox' || field.checked) {
          params.set(name, field.type === 'checkbox' ? 'on' : field.value);
        }
      }
      pdfLink.href = `${pdfLink.pathname}?${params}`;
    }
    return;
  }

});

document.addEventListener('DOMContentLoaded', () => {
  const automaticRange = document.getElementById('automatic-range');
  if (automaticRange) {
    const manualFields = document.querySelectorAll('.manual-range input');
    const autoScale = document.getElementById('auto-scale');
    const scale = document.querySelector('[name="scale"]');
    const autoOffset = document.getElementById('auto-offset');
    const offset = document.querySelector('[name="offset_s"]');
    const syncFields = () => {
      manualFields.forEach((field) => { field.disabled = automaticRange.checked; });
      if (scale) scale.disabled = autoScale?.checked ?? false;
      if (offset) offset.disabled = autoOffset?.checked ?? false;
    };
    automaticRange.addEventListener('change', syncFields);
    autoScale?.addEventListener('change', syncFields);
    autoOffset?.addEventListener('change', syncFields);
    syncFields();
  }

  if (!window.EmgCharts) return;

  const session = readAnalysisData('session-chart-data');
  if (session) {
    const chartColor = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    window.EmgCharts.bars('emg-average-chart', session.emg_bars, '各肌肉 RMS 平均值', 'mean');
    window.EmgCharts.bars('emg-peak-chart', session.emg_bars, '各肌肉 RMS Peak 值', 'peak');
    window.EmgCharts.selectableEnvelope('emg-envelope-chart', 'envelope-muscle', session.emg_envelopes);
    window.EmgCharts.pedalSeries('pedal-power-chart', session.pedal_power_series, 'W', session.average_power_w, session.maximum_power_w, chartColor('--chart-power'));
    window.EmgCharts.pedalSeries('pedal-cadence-chart', session.pedal_cadence_series, 'rpm', session.average_cadence_rpm, session.maximum_cadence_rpm, chartColor('--chart-cadence'));
    window.EmgCharts.pedalSeries('pedal-balance-chart', session.pedal_balance_series, '%', null, null, chartColor('--chart-balance'), true);
  }

  const angleProfiles = readAnalysisData('database-angle-profiles');
  if (angleProfiles) {
    window.EmgCharts.angleProfiles('database-angle-chart', 'database-angle-muscle', angleProfiles);
    window.setTimeout(() => window.dispatchEvent(new Event('resize')), 0);
  }

  const comparisonRows = readAnalysisData('comparison-chart-data');
  if (comparisonRows) window.EmgCharts.comparison('comparison-rms-chart', comparisonRows);

  const angleReport = readAnalysisData('angle-chart-data');
  if (angleReport) window.QtAngleReport?.render(angleReport);

  // Angle report tabs share one expensive calculation.  Switch the report
  // panel in-place so moving between Qt-equivalent views does not redraw the
  // whole page or flash the navigation shell.  The server response is cached
  // for Back/Forward-like repeated view changes during this visit.
  const anglePageCache = new Map();
  const replaceAnglePanel = (html, targetUrl, pushState = true) => {
    const documentFragment = new DOMParser().parseFromString(html, 'text/html');
    const nextPanel = documentFragment.getElementById('angle-tab');
    const currentPanel = document.getElementById('angle-tab');
    if (!nextPanel || !currentPanel) return;
    currentPanel.querySelectorAll('.qt-chart').forEach(element => {
      window.echarts?.getInstanceByDom(element)?.dispose();
    });
    currentPanel.replaceWith(nextPanel);
    const nextData = documentFragment.getElementById('angle-chart-data');
    const currentData = document.getElementById('angle-chart-data');
    if (nextData && currentData) currentData.textContent = nextData.textContent;
    else if (nextData) document.body.append(nextData);
    else currentData?.remove();
    if (pushState) history.pushState({ anglePanel: true }, '', targetUrl);
    const data = readAnalysisData('angle-chart-data');
    if (data) window.QtAngleReport?.render(data);
  };

  document.addEventListener('click', async (event) => {
    const link = event.target.closest('.angle-view-link');
    if (!link || event.defaultPrevented || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    const targetUrl = link.href;
    const panel = document.getElementById('angle-tab');
    if (!panel || targetUrl === window.location.href) return;
    panel.classList.add('angle-tab-loading');
    try {
      let html = anglePageCache.get(targetUrl);
      if (!html) {
        const response = await fetch(targetUrl, { headers: { 'X-Requested-With': 'XMLHttpRequest' } });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        html = await response.text();
        anglePageCache.set(targetUrl, html);
      }
      replaceAnglePanel(html, targetUrl);
    } catch (error) {
      // A normal navigation still gives the user the report if a transient
      // network error interrupts the optional accelerated transition.
      window.location.assign(targetUrl);
    } finally {
      document.getElementById('angle-tab')?.classList.remove('angle-tab-loading');
    }
  });

  window.addEventListener('popstate', () => {
    if (!document.getElementById('angle-tab') || !window.location.search.includes('view=angle')) return;
    const targetUrl = window.location.href;
    const html = anglePageCache.get(targetUrl);
    if (html) replaceAnglePanel(html, targetUrl, false);
    else window.location.reload();
  });
});
