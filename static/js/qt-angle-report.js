(() => {
  const BLUE = '#1684d7';
  const RED = '#d13438';
  const ORANGE = '#d97706';
  const ANGLES = [0, 45, 90, 135, 180, 225, 270, 315];
  const ANGLE_COLORS = [BLUE, '#e95f54', '#7e57c2', '#00897b', '#f39c12', '#5c6bc0', '#43a047', '#c2185b'];
  const finite = (number) => Number.isFinite(Number(number)) ? Number(number) : null;
  const signed = (number, decimals = 1) => number == null ? '—' : `${number >= 0 ? '+' : ''}${Number(number).toFixed(decimals)}`;
  const rowsBy = (rows, channel, phase) => rows.filter(row => row.channel === channel && row.phase === phase)
    .sort((a, b) => a.angle_bin_center - b.angle_bin_center);
  const name = row => `${row.location_zh} · ${row.channel_label}`;
  const average = values => values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : null;
  const rolling = (values, span, minimum = 1) => values.map((_, index) => {
    const valid = values.slice(Math.max(0, index - Math.floor(span / 2)), index + Math.ceil(span / 2))
      .filter(value => value != null && Number.isFinite(value));
    return valid.length >= minimum ? average(valid) : null;
  });
  const circularDistance = (a, b) => Math.abs(((a - b + 540) % 360) - 180);
  const interpolate = (profile, angle) => {
    if (!profile.length) return null;
    const center = ((angle % 360) + 360) % 360;
    const padded = [profile.at(-1), ...profile, profile[0]];
    for (let index = 0; index < padded.length - 1; index++) {
      const left = index === 0 ? padded[index].angle_bin_center - 360 : padded[index].angle_bin_center;
      const right = index === padded.length - 2 ? padded[index + 1].angle_bin_center + 360 : padded[index + 1].angle_bin_center;
      const position = center < profile[0].angle_bin_center ? center + 360 : center;
      if (left <= position && position <= right) {
        const leftValue = finite(padded[index].mean_percent);
        const rightValue = finite(padded[index + 1].mean_percent);
        return leftValue == null || rightValue == null ? null : leftValue + (rightValue - leftValue) * (position - left) / (right - left);
      }
    }
    return null;
  };
  const phaseAverage = (profile, angle) => average(profile.filter(row => circularDistance(row.angle_bin_center, angle) <= 15 + 1e-9)
    .map(row => finite(row.mean_percent)).filter(value => value != null));
  const chart = id => {
    const element = document.getElementById(id);
    if (!element || !window.echarts) return null;
    const instance = echarts.getInstanceByDom(element) || echarts.init(element);
    if (window.ResizeObserver) new ResizeObserver(() => instance.resize()).observe(element);
    window.addEventListener('resize', () => instance.resize(), { passive: true });
    requestAnimationFrame(() => instance.resize());
    return instance;
  };
  const toolbox = (instance, title) => window.EmgCharts.toolbox(instance, title);
  const line = (title, series, instance, yName = '%', max = 105) => ({
    title: { text: title, left: 'center', top: 8, textStyle: { fontSize: 14, lineHeight: 20 } },
    toolbox: toolbox(instance, title),
    tooltip: { trigger: 'axis' }, legend: { type: 'scroll', top: 62 },
    grid: { left: 75, right: 28, top: 105, bottom: 65, containLabel: true },
    xAxis: { type: 'value', min: 0, max: 360, name: '踩踏角度（°）', nameLocation: 'middle', nameGap: 32 },
    yAxis: { type: 'value', min: 0, max, name: yName, nameLocation: 'middle', nameGap: 48 }, series,
  });
  const selection = (id, summary, changed, all = false) => {
    const element = document.getElementById(id);
    if (!element) return;
    if (all) element.add(new Option('所有肌肉', ''));
    const preferred = ['RF', 'VM', 'VL', 'BF', 'GAS', 'SOL', 'TA'];
    const muscles = [...new Set(summary.map(row => row.muscle).filter(Boolean))];
    muscles.sort((a, b) => (preferred.indexOf(a) < 0 ? 99 : preferred.indexOf(a)) - (preferred.indexOf(b) < 0 ? 99 : preferred.indexOf(b)));
    muscles.forEach(muscle => {
      const row = summary.find(item => item.muscle === muscle);
      element.add(new Option(`${row.location_zh.slice(1)} (${muscle})`, muscle));
    });
    element.addEventListener('change', () => changed(element.value));
    changed(element.value);
  };
  const chartSeries = (profile, phase, label) => ({
    name: label, type: 'line', showSymbol: false,
    lineStyle: { width: 2, color: phase === 'start' ? BLUE : RED },
    itemStyle: { color: phase === 'start' ? BLUE : RED },
    data: profile.map(row => [row.angle_bin_center, finite(row.mean_percent)]),
  });

  window.QtAngleReport = {
    render(data) {
      if (!data || !window.echarts) return;
      const summary = data.summary || [];
      const profiles = data.profiles || [];
      const cycles = data.cycles || [];
      if (!summary.length) return;
      const byChannel = (channel, phase) => rowsBy(profiles, channel, phase);
      const phaseKey = `angle-phase:${location.pathname}`;
      const selectedPhase = () => Number(sessionStorage.getItem(phaseKey) || 0);

      const rmsChart = chart('angle-rms-change');
      const mdfChart = chart('angle-mdf-change');
      if (rmsChart || mdfChart) {
        const preferred = ['RF', 'VM', 'VL', 'BF', 'GAS', 'SOL', 'TA'];
        const muscles = [...new Set(summary.map(row => row.muscle))].sort((a, b) =>
          (preferred.indexOf(a) < 0 ? 99 : preferred.indexOf(a)) - (preferred.indexOf(b) < 0 ? 99 : preferred.indexOf(b)));
        const labels = muscles.map(muscle => {
          const row = summary.find(item => item.muscle === muscle);
          return `${row.location_zh.slice(1)}\n(${muscle})`;
        });
        for (const [instance, field, title, unit] of [[rmsChart, 'rms_change_percent', 'RMS 變化（後段比前段）', '%'], [mdfChart, 'mdf_change_hz', 'MDF 變化（後段減前段）', 'Hz']]) {
          if (!instance) continue;
          instance.setOption({ title: { text: title, left: 'center', top: 8 },
            toolbox: toolbox(instance, title), tooltip: { trigger: 'axis' },
            legend: { top: 42 }, grid: { left: 65, right: 25, top: 78, bottom: 76, containLabel: true },
            xAxis: { type: 'category', data: labels, axisLabel: { interval: 0, formatter: value => value.replace('\n', '\n') } },
            yAxis: { type: 'value', name: unit, scale: true },
            series: [['L', '左腳', BLUE], ['R', '右腳', ORANGE]].map(([side, label, color]) => ({
              name: label, type: 'bar', itemStyle: { color }, barMaxWidth: 36,
              label: { show: true, position: 'outside', formatter: item => item.value == null ? '' : `${signed(item.value)}${unit}` },
              data: muscles.map(muscle => finite(summary.find(row => row.muscle === muscle && row.side === side)?.[field])),
            })) }, true);
        }
      }

      selection('angle-fatigue-muscle', summary, muscle => {
        for (const [id, field, unit] of [['angle-fatigue-rms', 'rms_uv', 'µV'], ['angle-fatigue-mdf', 'mdf_hz', 'Hz']]) {
          const instance = chart(id); if (!instance) continue;
          const series = [['L', '左腳', BLUE], ['R', '右腳', ORANGE]].map(([side, label, color]) => {
            const rows = cycles.filter(row => row.side === side && row.muscle === muscle).sort((a, b) => a.time_s - b.time_s);
            const values = rolling(rows.map(row => finite(row[field])), 9, 3);
            return { name: label, type: 'line', showSymbol: false, lineStyle: { color, width: 2 },
              data: values.map((value, index) => [index + 1, value]) };
          });
          const title = `${muscle}：逐圈 ${field === 'rms_uv' ? 'RMS' : 'MDF'} 趨勢`;
          instance.setOption({ title: { text: `${title}\n9 圈移動平均`, left: 'center', top: 8, textStyle: { fontSize: 14, lineHeight: 20 } },
            toolbox: toolbox(instance, title),
            tooltip: { trigger: 'axis' }, legend: { top: 58 }, grid: { left: 65, right: 24, top: 94, bottom: 60, containLabel: true },
            xAxis: { type: 'value', name: '踩踏圈數', min: 1, nameLocation: 'middle', nameGap: 32 },
            yAxis: { type: 'value', name: unit }, series }, true);
        }
      });

      const detailSelect = document.getElementById('angle-detail-muscle');
      if (detailSelect) {
        summary.forEach(row => detailSelect.add(new Option(`${row.location_zh} (${row.muscle}) · ${row.channel_label}`, row.channel)));
        const saved = sessionStorage.getItem(`angle-muscle:${location.pathname}`);
        if (saved && summary.some(row => row.channel === saved)) detailSelect.value = saved;
        const draw = () => {
          const entry = summary.find(row => row.channel === detailSelect.value);
          if (!entry) return;
          sessionStorage.setItem(`angle-muscle:${location.pathname}`, entry.channel);
          document.getElementById('angle-detail-label').textContent = `${entry.location_zh}｜前段 ${entry.n_start} 圈、後段 ${entry.n_end} 圈。RMS：${entry.rms_uv_start.toFixed(1)} → ${entry.rms_uv_end.toFixed(1)} µV（${signed(entry.rms_change_percent)}%）；峰值：${entry.peak_angle_start.toFixed(0)}° → ${entry.peak_angle_end.toFixed(0)}°（${signed(entry.peak_angle_shift, 0)}°）。`;
          const series = [['start', '前段', entry.peak_angle_start], ['end', '後段', entry.peak_angle_end]].map(([phase, label, peak]) => ({
            ...chartSeries(byChannel(entry.channel, phase), phase, `${label} ${phase === 'start' ? entry.n_start : entry.n_end} 圈，峰值 ${peak.toFixed(0)}°`),
            markLine: { silent: true, symbol: 'none', label: { show: false }, data: [{ xAxis: peak, lineStyle: { type: 'dashed', color: phase === 'start' ? BLUE : RED } }] },
          }));
          series[0].markLine.data.push({ xAxis: selectedPhase(), lineStyle: { type: 'dotted', color: '#555555' } });
          const detailChart = chart('angle-detail-chart');
          detailChart?.setOption(line(`${entry.location_zh}｜RMS ${signed(entry.rms_change_percent)}%｜峰值相位 ${entry.peak_angle_start.toFixed(0)}° → ${entry.peak_angle_end.toFixed(0)}°（${signed(entry.peak_angle_shift, 0)}°）`, series, detailChart), true);
        };
        detailSelect.addEventListener('change', draw); draw();
      }

      const phaseInput = document.getElementById('angle-phase-input');
      if (phaseInput) {
        phaseInput.value = String(selectedPhase());
        const draw = () => {
          const degree = Math.max(0, Math.min(359, Number(phaseInput.value) || 0));
          phaseInput.value = String(degree); sessionStorage.setItem(phaseKey, String(degree));
          const rows = summary.map(row => ({ ...row, start: interpolate(byChannel(row.channel, 'start'), degree), end: interpolate(byChannel(row.channel, 'end'), degree) }))
            .sort((a, b) => (b.end ?? -1) - (a.end ?? -1) || (b.start ?? -1) - (a.start ?? -1));
          const highestStart = [...rows].sort((a, b) => (b.start ?? -1) - (a.start ?? -1))[0];
          const highestEnd = rows[0];
          document.getElementById('angle-phase-description').textContent = `${data.angle_label} ${degree}°｜後段相對活化最高：${highestEnd.location_zh}（${highestEnd.channel_label}）${Math.round(highestEnd.end)}%；前段相對活化最高：${highestStart.location_zh}（${highestStart.channel_label}）${Math.round(highestStart.start)}%。藍色為前段、紅色為後段。此為各肌肉內的相對 EMG 活化，不代表哪條肌肉實際最費力。`;
          const phaseChart = chart('angle-phase-chart');
          phaseChart?.setOption({ title: { text: `${degree}° 肌肉相對 EMG 活化｜藍色：前段；紅色：後段`, left: 'center', top: 8, textStyle: { fontSize: 15 } },
            toolbox: toolbox(phaseChart, `${degree}° 肌肉相對 EMG 活化`),
            tooltip: { trigger: 'axis' }, legend: { top: 42 }, grid: { left: 260, right: 28, top: 80, bottom: 56, containLabel: true },
            xAxis: { type: 'value', min: 0, max: 105, name: '相對自身參考值（%）', nameLocation: 'middle', nameGap: 32 },
            yAxis: { type: 'category', inverse: true, data: rows.map(name), axisLabel: { width: 230, overflow: 'truncate' } },
            series: [{ name: '前段', type: 'bar', barMaxWidth: 12, itemStyle: { color: BLUE }, data: rows.map(row => row.start) },
              { name: '後段', type: 'bar', barMaxWidth: 12, itemStyle: { color: RED }, data: rows.map(row => row.end) }] }, true);
        };
        phaseInput.addEventListener('change', draw); phaseInput.addEventListener('input', draw); draw();
      }

      const heatmap = chart('angle-heatmap-chart');
      if (heatmap) {
        const bins = [...new Set(profiles.filter(row => row.phase === 'all').map(row => row.angle_bin_center))].sort((a, b) => a - b);
        const channels = summary.map(row => row.channel);
        const channelIndex = new Map(channels.map((channel, index) => [channel, index]));
        const binIndex = new Map(bins.map((degree, index) => [degree, index]));
        const matrix = profiles.filter(row => row.phase === 'all').map(row => [binIndex.get(row.angle_bin_center), channelIndex.get(row.channel), finite(row.mean_percent)])
          .filter(row => row[0] != null && row[1] != null && row[2] != null);
        heatmap.setOption({ title: { text: '全肌肉角度熱圖｜每列僅比較自身訊號', left: 'center', top: 8 },
          toolbox: toolbox(heatmap, '全肌肉角度熱圖'),
          tooltip: { formatter: item => `${name(summary[item.value[1]])}<br>${bins[item.value[0]]}°：${item.value[2].toFixed(1)}%` },
          grid: { left: 260, right: 35, top: 66, bottom: 82, containLabel: true },
          xAxis: { type: 'category', data: bins, name: `${data.angle_label}（°）`, nameLocation: 'middle', nameGap: 35,
            axisLabel: { interval: 2, formatter: (_value, index) => `${index * 15}°` } },
          yAxis: { type: 'category', inverse: true, data: summary.map(name), axisLabel: { width: 230, overflow: 'truncate' } },
          // CET-L4 from PyQtGraph / ColorCET (CC BY 4.0), sampled at nine stops:
          // https://github.com/pyqtgraph/pyqtgraph/blob/master/pyqtgraph/colors/maps/CET-L4.csv
          visualMap: { min: 0, max: 100, show: false, inRange: { color: ['#000000', '#490100', '#7a0200', '#b00600', '#e71000', '#fb6200', '#fd9e00', '#ffd100', '#ffff00'] } },
          series: [{ type: 'heatmap', data: matrix, progressive: 0, itemStyle: { borderWidth: 0 },
            markLine: { silent: true, symbol: 'none', lineStyle: { color: '#555', type: 'dashed' }, label: { show: false },
              data: [{ xAxis: Math.min(bins.length - 1, Math.floor(selectedPhase() / (360 / bins.length))) }] } }] }, true);
      }

      selection('angle-profile-muscle', summary, muscle => {
        for (const [side, id] of [['L', 'angle-profile-left'], ['R', 'angle-profile-right']]) {
          const instance = chart(id); if (!instance) continue;
          const entry = summary.find(row => row.muscle === muscle && row.side === side);
          instance.setOption(entry ? line(`${entry.location_zh} ${entry.channel_label}  |  RMS ${signed(entry.rms_change_percent)}%`,
            [chartSeries(byChannel(entry.channel, 'start'), 'start', '前段'), chartSeries(byChannel(entry.channel, 'end'), 'end', '後段')], instance, '相對活化（%）') : {
            title: { text: `${side === 'L' ? '左腳' : '右腳'}：無此肌肉資料`, left: 'center' }, series: [], xAxis: {}, yAxis: {},
          }, true);
        }
      });

      selection('angle-polar-muscle', summary, muscle => {
        const container = document.getElementById('angle-polar-grid'); if (!container) return;
        container.replaceChildren();
        summary.filter(row => !muscle || row.muscle === muscle)
          .sort((a, b) => a.side.localeCompare(b.side) || a.location_zh.localeCompare(b.location_zh) || a.channel_label.localeCompare(b.channel_label))
          .forEach((row, index) => {
            const element = document.createElement('div');
            element.id = `angle-polar-${index}`; element.className = 'qt-chart angle-polar-card';
            container.append(element);
            const polarChart = chart(element.id);
            const polarTools = toolbox(polarChart, `${name(row)}圓形角度圖`);
            polarTools.top = 88;
            polarTools.right = 10;
            polarChart?.setOption({ title: { text: name(row), left: 'center', top: 12, textStyle: { fontSize: 13, lineHeight: 17 },
                width: '94%', overflow: 'break' },
              toolbox: polarTools,
              legend: { top: 58, left: 'center', itemGap: 24 }, tooltip: { trigger: 'item' },
              polar: { center: ['50%', '63%'], radius: '48%' },
              angleAxis: { type: 'value', min: 0, max: 360, interval: 45, startAngle: 90, clockwise: true,
                axisLabel: { margin: 9 } },
              radiusAxis: { min: 0, max: 105, interval: 25, axisLabel: { show: true } },
              series: [['start', '前段', BLUE], ['end', '後段', RED]].map(([phase, label, color]) => {
                const points = byChannel(row.channel, phase).map(value => [finite(value.mean_percent), value.angle_bin_center]);
                if (points.length) points.push([points[0][0], points[0][1] + 360]);
                return { name: label, type: 'line', coordinateSystem: 'polar', showSymbol: false,
                  lineStyle: { color, width: 2 }, itemStyle: { color }, data: points };
              }) }, true);
          });
      }, true);

      const cycleMuscle = document.getElementById('angle-cycle-muscle');
      const cycleAngle = document.getElementById('angle-cycle-angle');
      const drawCycles = muscle => {
        const selected = cycleAngle?.value;
        for (const [side, id] of [['L', 'angle-cycle-left'], ['R', 'angle-cycle-right']]) {
          const instance = chart(id); if (!instance) continue;
          const entry = summary.find(row => row.muscle === muscle && row.side === side);
          const rows = entry ? cycles.filter(row => row.channel === entry.channel).sort((a, b) => a.cycle - b.cycle) : [];
          const angles = selected === '' ? ANGLES : [Number(selected)];
          const centers = Array.from({ length: data.bins }, (_, index) => (index + .5) * 360 / data.bins);
          const series = angles.map(degree => {
            const columns = centers.flatMap((center, index) => circularDistance(center, degree) <= 15 + 1e-9 ? [`bin_${index}`] : []);
            const values = rolling(rows.map(row => average(columns.map(column => finite(row[column])).filter(value => value != null))), 5);
            return { name: `${degree}°`, type: 'line', showSymbol: false, lineStyle: { width: 2, color: ANGLE_COLORS[ANGLES.indexOf(degree)] },
              data: values.map((value, index) => [index + 1, value]) };
          });
          const start = rows.filter(row => row.phase === 'start').length;
          const end = rows.findIndex(row => row.phase === 'end');
          if (series.length) series[0].markArea = { silent: true, itemStyle: { color: '#1684d718' }, data: [
            ...(start ? [[{ xAxis: 1 }, { xAxis: start }]] : []),
            ...(end >= 0 ? [[{ xAxis: end + 1, itemStyle: { color: '#d1343818' } }, { xAxis: rows.length }]] : []),
          ] };
          instance.setOption({ title: { text: entry ? `${entry.location_zh} ${entry.channel_label}\n${selected === '' ? '逐圈 8 角度活化' : `${selected}°逐圈活化`}（5 圈移動平均）` : `${side === 'L' ? '左腳' : '右腳'}：無此肌肉資料`, left: 'center', top: 8, textStyle: { fontSize: 14, lineHeight: 20 } },
            toolbox: toolbox(instance, entry ? `${entry.location_zh}逐圈角度活化` : `${side}逐圈角度活化`),
            tooltip: { trigger: 'axis' }, legend: { type: 'scroll', top: 62 }, grid: { left: 55, right: 25, top: 103, bottom: 65, containLabel: true },
            xAxis: { type: 'value', min: 1, name: '踩踏圈數', nameLocation: 'middle', nameGap: 32 },
            yAxis: { type: 'value', min: 0, max: 105, name: '相對活化（%）' }, series }, true);
        }
      };
      selection('angle-cycle-muscle', summary, drawCycles);
      cycleAngle?.addEventListener('change', () => drawCycles(cycleMuscle.value));

      selection('angle-value-muscle', summary, muscle => {
        const body = document.getElementById('angle-value-rows'); if (!body) return;
        const bySide = side => summary.find(row => row.muscle === muscle && row.side === side);
        const sides = ['L', 'R'].map(bySide);
        const display = value => value == null ? '—' : `${value.toFixed(1)}%`;
        const displayDelta = (start, end) => start == null || end == null ? '—' : `${signed(end - start)}%`;
        const values = ANGLES.map(degree => {
          const row = [`${degree}° (±15°)`];
          sides.forEach(entry => {
            const start = entry && phaseAverage(byChannel(entry.channel, 'start'), degree);
            const end = entry && phaseAverage(byChannel(entry.channel, 'end'), degree);
            row.push(display(start), display(end), displayDelta(start, end));
          });
          return row;
        });
        for (const [label, first, last, unit] of [['RMS', 'rms_uv_start', 'rms_uv_end', ' µV'], ['MDF', 'mdf_start', 'mdf_end', ' Hz']]) {
          const row = [label];
          sides.forEach(entry => {
            if (!entry) { row.push('—', '—', '—'); return; }
            row.push(`${entry[first].toFixed(1)}${unit}`, `${entry[last].toFixed(1)}${unit}`, `${signed(entry[last] - entry[first])}${unit}`);
          }); values.push(row);
        }
        body.replaceChildren(...values.map(valuesRow => {
          const tr = document.createElement('tr');
          valuesRow.forEach(value => { const td = document.createElement('td'); td.textContent = value; tr.append(td); });
          return tr;
        }));
      });

      document.querySelectorAll('.angle-comparison-row').forEach(row => {
        const open = () => {
          sessionStorage.setItem(`angle-muscle:${location.pathname}`, row.dataset.channel);
          document.querySelector('.angle-view-link[href*="angle_section=muscle"]')?.click();
        };
        row.addEventListener('click', open);
        row.addEventListener('keydown', event => { if (event.key === 'Enter') open(); });
      });
    },
  };
})();
