(() => {
  const palette = [
    '#5470c6', '#91cc75', '#fac858', '#ee6666', '#73c0de', '#3ba272', '#fc8452',
    '#9a60b4', '#ea7ccc', '#2f4554', '#61a0a8', '#d48265', '#749f83', '#ca8622',
    '#bda29a', '#6e7074',
  ];
  const bindResize = (chart) => {
    window.addEventListener('resize', () => chart.resize(), { passive: true });
    document.addEventListener('shown.bs.tab', () => chart.resize());
  };
  const base = (title) => ({
    title: { text: title, left: 8, top: 8 },
    tooltip: { trigger: 'axis' },
    legend: { type: 'scroll', top: 42 },
    toolbox: {
      show: true,
      right: 10,
      top: 8,
      feature: {
        dataView: { show: true, readOnly: false },
        dataZoom: { show: true },
        restore: { show: true },
        saveAsImage: { show: true },
      },
    },
    grid: { left: 56, right: 24, top: 82, bottom: 78 },
    dataZoom: [
      { type: 'inside', xAxisIndex: 0, filterMode: 'none', moveOnMouseMove: true, zoomOnMouseWheel: true },
      { type: 'slider', xAxisIndex: 0, height: 16, bottom: 16, filterMode: 'none' },
    ],
    backgroundColor: 'transparent',
  });

  window.EmgCharts = {
    timeSeries(id, series) {
      const element = document.getElementById(id);
      if (!element || !window.echarts || !series?.length) return;
      const chart = echarts.init(element, null, { renderer: 'canvas' });
      const option = base('EMG RMS 時序');
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
      const option = base(title);
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
      const option = base('');
      delete option.title;
      option.legend = { show: scatter, top: 6, left: 'center' };
      option.grid = { left: 56, right: 24, top: scatter ? 42 : 52, bottom: 78 };
      option.xAxis = { type: 'value', name: '秒', nameLocation: 'middle', nameGap: 30 };
      option.yAxis = { type: 'value', name: unit, min: unit === '%' ? 0 : undefined,
        max: unit === '%' ? 100 : undefined };
      option.series = series.map((item, index) => ({
        name: item.name,
        type: scatter ? 'scatter' : 'line',
        showSymbol: scatter,
        symbolSize: scatter ? 5 : 0,
        smooth: !scatter,
        connectNulls: false,
        lineStyle: { width: 2, color: scatter ? (index ? '#1684d7' : '#43a047') : color },
        itemStyle: { color: scatter ? (index ? '#1684d7' : '#43a047') : color },
        areaStyle: scatter ? undefined : { color: `${color}26` },
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
      const option = base('');
      delete option.title;
      delete option.legend;
      option.grid = { left: 54, right: 22, top: 52, bottom: 112 };
      option.xAxis = { type: 'category', data: rows.map(row => row.label),
        axisLabel: { interval: 0, rotate: 38, margin: 14,
          formatter: label => {
            const parts = String(label).split(' ');
            return `${parts.shift()} ${parts.join(' ')}`;
          } } };
      option.yAxis = { type: 'value', name: 'mV' };
      option.series = [{ name: title, type: 'bar', barMaxWidth: 48, itemStyle: { color: palette[0] },
        label: { show: true, position: 'top', formatter: value => value.value == null ? '—' : Number(value.value).toFixed(3) },
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
        const option = base('');
        delete option.title;
        delete option.legend;
        option.grid = { left: 56, right: 24, top: 52, bottom: 78 };
        option.xAxis = { type: 'value', name: '秒', nameLocation: 'middle', nameGap: 30 };
        option.yAxis = { type: 'value', name: 'mV' };
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
        const option = base('');
        delete option.title;
        option.legend = { show: true, top: 6, left: 'center' };
        option.grid = { left: 56, right: 24, top: 42, bottom: 78 };
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
      const option = base('兩次測試 RMS 中位數比較');
      option.xAxis = { type: 'category', data: rows.map(row => row.label), axisLabel: { rotate: 35 } };
      option.yAxis = { type: 'value', name: 'mV' };
      option.series = [
        { name: '量測 A', type: 'bar', itemStyle: { color: palette[0] }, data: rows.map(row => row.first) },
        { name: '量測 B', type: 'bar', itemStyle: { color: palette[1] }, data: rows.map(row => row.second) },
      ];
      chart.setOption(option);
      bindResize(chart);
    },
  };
})();
