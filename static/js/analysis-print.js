/* Build the report from the same rendered panels used by the analysis screen. */
(() => {
  const pages = JSON.parse(document.getElementById('report-config').textContent);
  const output = document.getElementById('report-pages');
  const progress = document.getElementById('report-progress');
  const printButton = document.getElementById('report-print');
  printButton.addEventListener('click', () => window.print());
  const nextPaint = win => new Promise(resolve => win.requestAnimationFrame(() => win.requestAnimationFrame(resolve)));

  async function snapshot(frame, panel) {
    const win = frame.contentWindow;
    await win.document.fonts.ready;
    const charts = [...panel.querySelectorAll('.qt-chart')];
    for (const element of charts) {
      const chart = win.echarts?.getInstanceByDom(element);
      if (chart) {
        chart.setOption({ animation: false });
        chart.resize();
      }
    }
    await nextPaint(win);
    const clone = panel.cloneNode(true);
    const copies = [...clone.querySelectorAll('.qt-chart')];
    charts.forEach((element, index) => {
      const chart = win.echarts?.getInstanceByDom(element);
      if (!chart) return;
      const figure = document.createElement('figure');
      figure.className = 'report-chart-figure';
      const title = chart.getOption().toolbox?.[0]?.feature?.saveAsImage?.name;
      if (title) {
        const caption = document.createElement('figcaption');
        caption.textContent = title;
        figure.append(caption);
      }
      const image = document.createElement('img');
      image.className = 'report-chart';
      image.alt = title || '分析圖表';
      image.src = chart.getDataURL({ type: 'png', pixelRatio: 2, backgroundColor: '#fff', excludeComponents: ['toolbox'] });
      figure.append(image);
      copies[index].replaceWith(figure);
    });
    // Keep the selected settings as text in the PDF instead of editable controls.
    clone.querySelectorAll('select').forEach(select => {
      const text = document.createElement('span');
      const original = panel.querySelector(`#${CSS.escape(select.id)}`);
      text.textContent = original?.selectedOptions[0]?.textContent || '';
      select.replaceWith(text);
    });
    clone.querySelectorAll('input:not([type="hidden"])').forEach(input => {
      const text = document.createElement('span');
      const original = panel.querySelector(`#${CSS.escape(input.id)}`);
      text.textContent = original?.value || input.value;
      input.replaceWith(text);
    });
    clone.querySelectorAll('button, input[type="hidden"], .angle-view-nav, .modal, script').forEach(node => node.remove());
    clone.querySelectorAll('[id]').forEach(node => node.removeAttribute('id'));
    clone.removeAttribute('id');
    return clone;
  }

  async function loadFrame(url) {
    const frame = document.createElement('iframe');
    frame.className = 'report-source-frame';
    frame.title = '載入報告分析';
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('分析頁載入逾時，請重新整理後重試。')), 180000);
      frame.onload = () => { clearTimeout(timer); resolve(); };
      frame.onerror = () => { clearTimeout(timer); reject(new Error('無法載入分析頁。')); };
      frame.src = url;
      document.body.append(frame);
    }).catch(error => { frame.remove(); throw error; });
    return frame;
  }

  const variants = {
    emg: 'envelope-muscle', angle_muscle: 'angle-detail-muscle',
    angle_fatigue_trend: 'angle-fatigue-muscle', angle_profiles: 'angle-profile-muscle',
    angle_cycle8: 'angle-cycle-muscle', angle_values: 'angle-value-muscle',
  };

  async function build() {
    for (const [index, page] of pages.entries()) {
      progress.textContent = `正在整理 ${index + 1}／${pages.length}：${page.label}`;
      const frame = await loadFrame(page.url);
      try {
        const doc = frame.contentDocument;
        const view = page.key.startsWith('angle_') ? 'angle' : page.key;
        const panel = doc.getElementById(`${view}-tab`);
        if (!panel) throw new Error(`${page.label}載入失敗，請確認網站連線後重試。`);
        if (doc.querySelector('.qt-chart') && !frame.contentWindow.echarts) throw new Error('圖表元件未載入，請確認網路後重試。');
        if (index === 0) {
          const overview = doc.querySelector('.test-overview');
          if (overview) {
            const section = document.createElement('section');
            section.className = 'report-section';
            const title = document.createElement('h2'); title.textContent = '測試概況';
            section.append(title, overview.cloneNode(true)); output.append(section);
          }
        }
        const section = document.createElement('section'); section.className = 'report-section';
        const heading = document.createElement('h2'); heading.textContent = page.label; section.append(heading);
        const select = doc.getElementById(variants[page.key]);
        const choices = select ? [...select.options].filter(option => option.value !== '') : [];
        if (choices.length) {
          for (const [choiceIndex, choice] of choices.entries()) {
            select.value = choice.value;
            select.dispatchEvent(new frame.contentWindow.Event('change'));
            const part = document.createElement('div'); part.className = 'report-part';
            const label = document.createElement('h3'); label.textContent = choice.textContent; part.append(label);
            const copy = await snapshot(frame, panel);
            // The EMG summary and table appear once; only the envelope varies by muscle.
            if (page.key === 'emg' && choiceIndex > 0) {
              const envelope = copy.querySelector('.envelope-header')?.closest('.analysis-chart-card');
              if (envelope) part.append(envelope);
            } else part.append(copy);
            section.append(part);
          }
        } else {
          const polar = doc.getElementById('angle-polar-muscle');
          if (polar && [...polar.options].some(option => option.value === '')) {
            polar.value = ''; polar.dispatchEvent(new frame.contentWindow.Event('change'));
          }
          section.append(await snapshot(frame, panel));
        }
        output.append(section);
      } finally {
        // Enumerating muscle selections must not change the user's saved selection.
        frame.remove();
      }
    }
    await Promise.all([...output.querySelectorAll('img')].map(image => image.decode()));
    progress.textContent = `已完成 ${pages.length} 項分析。可預覽後列印／另存為橫式 PDF。`;
    printButton.disabled = false;
  }
  const muscleKey = `angle-muscle:${new URL(pages[0].url, location.href).pathname}`;
  const originalMuscle = sessionStorage.getItem(muscleKey);
  build().catch(error => {
    progress.textContent = `報告未完成：${error.message}`;
    progress.classList.add('text-danger');
  }).finally(() => {
    if (originalMuscle === null) sessionStorage.removeItem(muscleKey);
    else sessionStorage.setItem(muscleKey, originalMuscle);
  });
})();
