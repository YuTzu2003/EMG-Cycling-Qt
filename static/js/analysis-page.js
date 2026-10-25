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

  const link = event.target.closest('[data-history-back]');
  if (!link || !document.referrer || window.history.length < 2) return;
  try {
    const previous = new URL(document.referrer);
    if (previous.origin === window.location.origin && previous.href !== window.location.href) {
      event.preventDefault();
      window.history.back();
    }
  } catch (_error) {
    // The fallback href remains available when the referrer is invalid.
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
});
