(() => {
  const palette = [
    '#1684d7', '#e95f54', '#7e57c2', '#00897b', '#f39c12', '#5c6bc0', '#43a047', '#c2185b',
    '#00a6a6', '#d1495b', '#725ac1', '#6b8e23', '#d97706', '#0077b6', '#9d4edd', '#e76f51',
  ];
  const chartToolbox = (chart, title) => ({
    show: true, right: 12, top: 8, itemSize: 16, itemGap: 12,
    feature: {
      dataView: { show: true, readOnly: true, title: '預覽圖表資料', lang: ['圖表資料', '關閉', '重新整理'] },
      myDownloadCsv: {
        show: true, title: '下載圖表資料 CSV',
        icon: 'path://M5 20h14v-2H5v2zM19 9h-4V3H9v6H5l7 7 7-7z',
        onclick: () => {
          const option = chart.getOption();
          const xAxis = option.xAxis?.[0]?.data || [];
          const yAxis = option.yAxis?.[0]?.data || [];
          const rows = [['系列', 'X／角度', 'Y／肌肉', '數值']];
          (option.series || []).forEach((series) => {
            (series.data || []).forEach((point, index) => {
              const value = point && typeof point === 'object' && !Array.isArray(point) && 'value' in point ? point.value : point;
              let x = xAxis[index] ?? index + 1;
              let y = '';
              let number = value;
              if (Array.isArray(value)) {
                if (series.coordinateSystem === 'polar') {
                  [number, x] = value;
                } else if (series.type === 'heatmap') {
                  x = xAxis[value[0]] ?? value[0];
                  y = yAxis[value[1]] ?? value[1];
                  number = value[2];
                } else {
                  [x, number] = value;
                }
              }
              rows.push([series.name || title, x, y, number ?? '']);
            });
          });
          const csvCell = value => {
            const text = String(value ?? '');
            const safe = typeof value === 'string' && /^[\s]*[=+\-@]/.test(text) ? `'${text}` : text;
            return `"${safe.replaceAll('"', '""')}"`;
          };
          const csv = '\uFEFF' + rows.map(row => row.map(csvCell).join(',')).join('\r\n');
          const url = URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8' }));
          const link = document.createElement('a');
          link.href = url;
          link.download = `${title.replace(/[\\/:*?"<>|]/g, '_')}.csv`;
          document.body.append(link);
          link.click();
          link.remove();
          setTimeout(() => URL.revokeObjectURL(url), 1000);
        },
      },
      saveAsImage: { show: true, title: '下載圖表圖片', name: title.replace(/[\\/:*?"<>|]/g, '_'), pixelRatio: 2 },
    },
  });
  const bindResize = (chart) => {
    window.addEventListener('resize', () => chart.resize(), { passive: true });
    document.addEventListener('shown.bs.tab', () => chart.resize());
    // A report panel can change width without a browser resize (responsive
    // grid, modal close, or the in-place angle-tab transition).
    if (window.ResizeObserver) {
      const observer = new ResizeObserver(() => chart.resize());
      observer.observe(chart.getDom());
    }
    requestAnimationFrame(() => chart.resize());
  };
  const base = (title, chart) => ({
    title: { show: false },
    tooltip: { trigger: 'axis' },
    legend: { type: 'scroll', top: 42 },
    toolbox: chartToolbox(chart, title),
    grid: { left: 56, right: 24, top: 82, bottom: 78 },
    dataZoom: [
      { type: 'inside', xAxisIndex: 0, filterMode: 'none', moveOnMouseMove: true, zoomOnMouseWheel: true },
      { type: 'slider', xAxisIndex: 0, height: 16, bottom: 16, filterMode: 'none' },
    ],
    backgroundColor: 'transparent',
  });

  window.EmgCharts = {
    toolbox: chartToolbox,
    timeSeries(id, series) {
      const element = document.getElementById(id);
      if (!element || !window.echarts || !series?.length) return;
      const chart = echarts.init(element, null, { renderer: 'canvas' });
      const option = base('EMG RMS 時序', chart);
      option.xAxis = { type: 'value', name: '秒', nameLocation: 'middle', nameGap: 30 };
      option.yAxis = { type: 'value', name: 'mV' };
      option.series = series.map((item, index) => ({
        name: item.name, type: 'line', showSymbol: false, smooth: true,
        lineStyle: { width: 2, color: palette[index % palette.length] }, data: item.data,
      }));
      chart.setOption(option);
      bindResize(chart);
    },
    telemetry(id, series, title, unit) {
      const element = document.getElementById(id);
      if (!element || !window.echarts || !series?.length) return;
      const chart = echarts.init(element, null, { renderer: 'canvas' });
      const option = base(title, chart);
      option.xAxis = { type: 'value', name: '秒', nameLocation: 'middle', nameGap: 30 };
      option.yAxis = { type: 'value', name: unit };
      option.series = series.map((item, index) => ({
        name: item.name, type: 'line', showSymbol: false, smooth: true,
        lineStyle: { width: 2, color: palette[index % palette.length] }, data: item.data,
      }));
      chart.setOption(option);
      bindResize(chart);
    },
    pedalSeries(id, series, unit, average, maximum, color, scatter = false) {
      const element = document.getElementById(id);
      if (!element || !window.echarts || !series?.length) return;
      const chart = echarts.init(element, null, { renderer: 'canvas' });
      const title = scatter ? '左右平衡 Left / Right Balance' : unit === 'W' ? '功率 Power' : '踏頻 Cadence';
      const option = base(title, chart);
      option.title = { show: false };
      option.legend = { show: scatter, top: 58, left: 'center' };
      option.grid = { left: 64, right: 28, top: scatter ? 102 : 82, bottom: 68, containLabel: true };
      option.xAxis = { type: 'value', name: '時間（s）', nameLocation: 'middle', nameGap: 32 };
      option.yAxis = { type: 'value', name: unit === '%' ? '平衡（%）' : unit === 'W' ? '功率（W）' : '踏頻（rpm）', min: unit === '%' ? 0 : undefined,
        max: unit === '%' ? 100 : undefined };
      option.series = series.map((item, index) => ({
        name: scatter ? (index ? '右' : '左') : item.name,
        type: scatter ? 'scatter' : 'line',
        showSymbol: scatter,
        symbolSize: scatter ? 5 : 0,
        smooth: !scatter,
        connectNulls: false,
        lineStyle: { width: 2, color: scatter ? (index ? '#1684d7' : '#43a047') : color },
        itemStyle: { color: scatter ? (index ? '#1684d7' : '#43a047') : color },
        areaStyle: scatter ? undefined : { color: `${color}40` },
        markLine: !scatter && (average != null || maximum != null) ? {
          symbol: 'none', label: { color: '#6e6e73' }, data: [
            ...(average == null ? [] : [{ name: '平均', yAxis: average, lineStyle: { type: 'dashed', color: '#6e6e73' } }]),
            ...(maximum == null ? [] : [{ name: '最大', yAxis: maximum, lineStyle: { type: 'dotted', color } }]),
          ],
        } : undefined,
        data: item.data,
      }));
      chart.setOption(option);
      bindResize(chart);
    },
    bars(id, rows, title, field) {
      const element = document.getElementById(id);
      if (!element || !window.echarts || !rows?.length) return;
      const chart = echarts.init(element, null, { renderer: 'canvas' });
      const option = base(title, chart);
      delete option.legend;
      option.toolbox = chartToolbox(chart, title);
      option.dataZoom = [{ type: 'inside', xAxisIndex: 0, filterMode: 'none' }];
      option.tooltip = { trigger: 'item', formatter: item => `${rows[item.dataIndex].label}<br>${title}：${Number(item.value).toFixed(3)} mV` };
      option.grid = { left: 48, right: 24, top: 64, bottom: 60, containLabel: true };
      option.xAxis = { type: 'category', data: rows.map(row => String(row.label).split(' ')[0]), axisLabel: { interval: 0, rotate: 0, margin: 10 } };
      option.yAxis = { type: 'value', name: 'RMS（mV）', min: 0 };
      option.series = [{ name: title, type: 'bar', barMaxWidth: 48, itemStyle: { color: palette[0] },
        data: rows.map((row, index) => ({ value: row[field], itemStyle: { color: palette[index % palette.length] } })) }];
      chart.setOption(option);
      bindResize(chart);
    },
    selectableEnvelope(id, selectId, envelopes) {
      const element = document.getElementById(id);
      const select = document.getElementById(selectId);
      if (!element || !select || !window.echarts || !envelopes?.length) return;
      const chart = echarts.init(element, null, { renderer: 'canvas' });
      const draw = () => {
        const item = envelopes.find(value => value.slot === select.value) || envelopes[0];
        const option = base(item.label, chart);
        option.title = { show: false };
        delete option.legend;
        option.toolbox = chartToolbox(chart, `EMG RMS Peak ${item.label}`);
        option.grid = { left: 55, right: 26, top: 55, bottom: 68, containLabel: true };
        option.xAxis = { type: 'value', name: '騎乘時間（s）', nameLocation: 'middle', nameGap: 32 };
        option.yAxis = { type: 'value', name: 'RMS（mV）' };
        const colorIndex = Math.max(0, envelopes.findIndex(value => value.slot === item.slot));
        option.series = [{ name: item.label, type: 'line', showSymbol: false, smooth: false,
          lineStyle: { width: 2, color: palette[colorIndex % palette.length] },
          itemStyle: { color: palette[colorIndex % palette.length] }, data: item.data }];
        chart.setOption(option, true);
      };
      select.addEventListener('change', draw);
      draw();
      bindResize(chart);
    },
    angleProfiles(id, selectId, profiles) {
      const element = document.getElementById(id);
      const select = document.getElementById(selectId);
      if (!element || !select || !window.echarts || !profiles?.length) return;
      const chart = echarts.init(element, null, { renderer: 'canvas' });
      const draw = () => {
        const item = profiles.find(value => value.slot === select.value) || profiles[0];
        const option = base(item.label, chart);
        option.title = { show: false };
        option.legend = { show: true, top: 48, left: 'center' };
        option.grid = { left: 56, right: 24, top: 84, bottom: 78 };
        option.xAxis = { type: 'value', name: '踩踏角度（°）', min: 0, max: 360, nameLocation: 'middle', nameGap: 30 };
        option.yAxis = { type: 'value', name: 'RMS mV' };
        option.series = [
          { name: '前段', type: 'line', smooth: true, showSymbol: false,
            lineStyle: { width: 2, color: '#1684d7' }, data: item.early },
          { name: '後段', type: 'line', smooth: true, showSymbol: false,
            lineStyle: { width: 2, color: '#d13438' }, data: item.late },
        ];
        chart.setOption(option, true);
      };
      select.addEventListener('change', draw);
      draw();
      bindResize(chart);
    },
    comparison(id, rows) {
      const element = document.getElementById(id);
      if (!element || !window.echarts || !rows?.length) return;
      const chart = echarts.init(element, null, { renderer: 'canvas' });
      const option = base('兩次測試 RMS 中位數比較', chart);
      option.xAxis = { type: 'category', data: rows.map(row => row.label), axisLabel: { rotate: 35 } };
      option.yAxis = { type: 'value', name: 'mV' };
      option.series = [
        { name: '量測 A', type: 'bar', itemStyle: { color: palette[0] }, data: rows.map(row => row.first) },
        { name: '量測 B', type: 'bar', itemStyle: { color: palette[1] }, data: rows.map(row => row.second) },
      ];
      chart.setOption(option);
      bindResize(chart);
    },
    angleReport(data) {
      if (!data || !window.echarts) return;
      const summary = data.summary || [], profiles = data.profiles || [], cycles = data.cycles || [];
      const chart = (id) => {
        const element = document.getElementById(id);
        if (!element) return null;
        const instance = echarts.init(element, null, { renderer: 'canvas' });
        bindResize(instance);
        return instance;
      };
      const muscles = [...new Set(summary.map(row => row.muscle).filter(Boolean))];
      const label = (row) => `${row.location_zh || ''} ${row.channel_label || row.channel}`.trim();
      const addMuscles = (id, onChange) => {
        const select = document.getElementById(id);
        if (!select) return;
        muscles.forEach(muscle => select.add(new Option(muscle, muscle)));
        select.addEventListener('change', () => onChange(select.value));
        onChange(select.value || muscles[0]);
      };
      const profile = (channel, phase) => profiles.filter(row => row.channel === channel && row.phase === phase)
        .sort((a, b) => a.angle_bin_center - b.angle_bin_center).map(row => [row.angle_bin_center, row.mean_percent]);
      const lineOption = (title, series, yName = '%') => ({
        title: { show: false }, tooltip: { trigger: 'axis' }, legend: { top: 40 },
        grid: { left: 55, right: 25, top: 76, bottom: 42 },
        xAxis: { type: 'value', name: '角度（°）', min: 0, max: 360 }, yAxis: { type: 'value', name: yName, min: 0, max: yName === '%' ? 105 : undefined },
        series,
      });
      const fatigue = chart('angle-fatigue-chart');
      if (fatigue) {
        fatigue.setOption({ title: { show: false }, tooltip: { trigger: 'axis' }, legend: { top: 34 }, grid: { left: 55, right: 24, top: 72, bottom: 95 },
          xAxis: { type: 'category', data: summary.map(label), axisLabel: { rotate: 35 } }, yAxis: { type: 'value', name: '%' },
          series: ['L', 'R'].map((side, index) => ({ name: side === 'L' ? '左側' : '右側', type: 'bar', data: summary.map(row => row.side === side ? row.rms_change_percent : null), itemStyle: { color: index ? '#d97706' : '#1684d7' } })) });
      }
      addMuscles('angle-fatigue-muscle', (muscle) => {
        [['angle-fatigue-rms', 'rms_uv', 'RMS（µV）'], ['angle-fatigue-mdf', 'mdf_hz', 'MDF（Hz）']].forEach(([id, field, title]) => {
          const instance = chart(id); if (!instance) return;
          const series = ['L', 'R'].map((side, index) => {
            const rows = cycles.filter(row => row.muscle === muscle && row.side === side).sort((a, b) => a.cycle - b.cycle);
            const values = rows.map(row => row[field]);
            const smoothed = values.map((_, i) => {
              const window = values.slice(Math.max(0, i - 4), Math.min(values.length, i + 5)).filter(value => value != null);
              return window.length ? window.reduce((sum, value) => sum + value, 0) / window.length : null;
            });
            return { name: side === 'L' ? '左側（9 圈平滑）' : '右側（9 圈平滑）', type: 'line', showSymbol: false, smooth: true, itemStyle: { color: index ? '#d97706' : '#1684d7' }, data: smoothed.map((value, i) => [i + 1, value]) };
          });
          instance.setOption({ title: { show: false }, tooltip: { trigger: 'axis' }, legend: { top: 32 }, grid: { left: 55, right: 24, top: 70, bottom: 42 }, xAxis: { type: 'value', name: '踩踏圈數' }, yAxis: { type: 'value', name: field === 'rms_uv' ? 'µV' : 'Hz' }, series }, true);
        });
      });
      const phaseChart = chart('angle-phase-chart');
      const phaseTable = document.getElementById('angle-phase-table');
      const drawPhase = (angle) => {
        const values = summary.map(row => {
          const interpolate = (phase) => {
            const points = profile(row.channel, phase); if (!points.length) return null;
            let nearest = points[0]; let distance = 999;
            points.forEach(point => { const d = Math.abs(((point[0] - angle + 540) % 360) - 180); if (d < distance) { distance = d; nearest = point; } });
            return nearest[1];
          };
          return { ...row, start: interpolate('start'), end: interpolate('end') };
        }).sort((a, b) => (b.end || 0) - (a.end || 0));
        if (phaseChart) phaseChart.setOption({ title: { show: false }, tooltip: { trigger: 'axis' }, legend: { top: 34 }, grid: { left: 165, right: 28, top: 72, bottom: 36 }, xAxis: { type: 'value', name: '%' }, yAxis: { type: 'category', data: values.map(label) }, series: [{ name: '前段', type: 'bar', data: values.map(row => row.start), itemStyle: { color: '#1684d7' } }, { name: '後段', type: 'bar', data: values.map(row => row.end), itemStyle: { color: '#d13438' } }] }, true);
        if (phaseTable) phaseTable.innerHTML = `<table class="table table-sm data-table mb-0"><thead><tr><th>肌肉</th><th>前段</th><th>後段</th><th>變化</th></tr></thead><tbody>${values.map(row => `<tr><td>${label(row)}</td><td>${row.start?.toFixed(1) ?? '—'}%</td><td>${row.end?.toFixed(1) ?? '—'}%</td><td>${row.start != null && row.end != null ? (row.end - row.start).toFixed(1) : '—'}%</td></tr>`).join('')}</tbody></table>`;
      };
      const input = document.getElementById('angle-phase-input');
      if (input) { input.addEventListener('input', () => { document.getElementById('angle-phase-value').textContent = `${input.value}°`; drawPhase(Number(input.value)); }); drawPhase(Number(input.value)); }
      const heatmap = chart('angle-heatmap-chart');
      if (heatmap) {
        // Identical matrix layout to Qt: every row is one muscle and every
        // column is one of the 24 full-ride angle bins (not one diagonal cell).
        const channels = summary.map(row => row.channel);
        const names = summary.map(row => `${row.location_zh || ''} · ${row.channel_label || row.channel}`.trim());
        const indexByChannel = new Map(channels.map((channel, index) => [channel, index]));
        const bins = [...new Set(profiles.filter(row => row.phase === 'all').map(row => row.angle_bin_center))].sort((a, b) => a - b);
        const indexByBin = new Map(bins.map((bin, index) => [bin, index]));
        const points = profiles.filter(row => row.phase === 'all').map(row => [
          indexByBin.get(row.angle_bin_center), indexByChannel.get(row.channel), row.mean_percent,
        ]).filter(row => row[0] !== undefined && row[1] !== undefined);
        heatmap.setOption({
          title: { show: false },
          tooltip: { formatter: item => `${names[item.value[1]]}<br>${Number(item.value[0]).toFixed(1)}°：${Number(item.value[2]).toFixed(1)}%` },
          grid: { left: 255, right: 82, top: 65, bottom: 96 },
          xAxis: { type: 'category', data: bins, name: '相對踩踏相位（°）', nameLocation: 'middle', nameGap: 34,
            axisLabel: { interval: 2, formatter: (value, index) => index % 3 === 0 ? `${Math.round(value - 7.5)}°` : '' } },
          yAxis: { type: 'category', inverse: true, data: names, axisLabel: { width: 235, overflow: 'truncate' } },
          visualMap: { min: 0, max: 100, calculable: false, orient: 'horizontal', left: 'center', bottom: 14,
            text: ['100%', '0%'], inRange: { color: ['#000004', '#3b0f70', '#8c2981', '#de4968', '#fe9f6d', '#fcfdbf'] } },
          series: [{ type: 'heatmap', data: points, progressive: 0, itemStyle: { borderWidth: 0 },
            markLine: { silent: true, symbol: 'none', lineStyle: { color: '#555', type: 'dashed' }, data: [{ xAxis: bins[Math.floor(90 / (360 / bins.length))] }] } }],
        });
      }
      addMuscles('angle-polar-muscle', (muscle) => { const instance = chart('angle-polar-chart'); if (!instance) return; const entries = summary.filter(row => row.muscle === muscle); instance.setOption({ title: { show: false }, legend: { top: 35 }, polar: {}, angleAxis: { type: 'value', min: 0, max: 360, startAngle: 90 }, radiusAxis: { min: 0, max: 105 }, series: entries.flatMap((row, index) => ['start', 'end'].map(phase => ({ name: `${label(row)} ${phase === 'start' ? '前段' : '後段'}`, type: 'line', coordinateSystem: 'polar', data: profile(row.channel, phase), lineStyle: { color: palette[(index * 2 + (phase === 'end')) % palette.length] } }))) }, true); });
      addMuscles('angle-cycle-muscle', (muscle) => ['L', 'R'].forEach(side => { const instance = chart(`angle-cycle-${side === 'L' ? 'left' : 'right'}`); if (!instance) return; const entry = summary.find(row => row.muscle === muscle && row.side === side); const rows = entry ? cycles.filter(row => row.channel === entry.channel).sort((a,b) => a.cycle - b.cycle) : []; const angles = [0,45,90,135,180,225,270,315]; instance.setOption({ title: { show: false }, tooltip: { trigger: 'axis' }, legend: { type: 'scroll', top: 34 }, grid: { left: 52, right: 20, top: 72, bottom: 42 }, xAxis: { type: 'value', name: '圈數' }, yAxis: { type: 'value', name: '%' }, series: angles.map(angle => ({ name: `${angle}°`, type: 'line', showSymbol: false, data: rows.map((row, index) => [index + 1, row[`bin_${Math.floor(angle / (360 / data.bins))}`]]) })) }, true); }));
      [['angle-rms-change', 'rms_change_percent', 'RMS 變化（%）'], ['angle-mdf-change', 'mdf_change_hz', 'MDF 變化（Hz）']].forEach(([id, field, title]) => { const instance = chart(id); if (!instance) return; instance.setOption({ title: { show: false }, tooltip: { trigger: 'axis' }, grid: { left: 54, right: 20, top: 56, bottom: 95 }, xAxis: { type: 'category', data: summary.map(label), axisLabel: { rotate: 35 } }, yAxis: { type: 'value' }, series: [{ type: 'bar', data: summary.map(row => row[field]), itemStyle: { color: '#5470c6' } }] }); });
    },
  };
})();
