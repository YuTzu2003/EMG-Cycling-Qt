(() => {
  const palette = ['#0071e3', '#34c759', '#ff9f0a', '#af52de', '#ff375f', '#64d2ff'];
  const base = (title) => ({
    title: { text: title, left: 8, top: 8, textStyle: { fontSize: 16, fontWeight: 600 } },
    tooltip: { trigger: 'axis' },
    legend: { type: 'scroll', top: 42 },
    grid: { left: 56, right: 24, top: 82, bottom: 46 },
    backgroundColor: 'transparent',
  });

  window.EmgCharts = {
    timeSeries(id, series) {
      const element = document.getElementById(id);
      if (!element || !window.echarts || !series?.length) return;
      const chart = echarts.init(element);
      const option = base('EMG RMS 時序');
      option.xAxis = { type: 'value', name: '秒', nameLocation: 'middle', nameGap: 30 };
      option.yAxis = { type: 'value', name: 'mV' };
      option.series = series.map((item, index) => ({
        name: item.name, type: 'line', showSymbol: false, smooth: true,
        lineStyle: { width: 2, color: palette[index % palette.length] }, data: item.data,
      }));
      chart.setOption(option);
      window.addEventListener('resize', () => chart.resize(), { passive: true });
    },
    comparison(id, rows) {
      const element = document.getElementById(id);
      if (!element || !window.echarts || !rows?.length) return;
      const chart = echarts.init(element);
      const option = base('兩次測試 RMS 中位數比較');
      option.xAxis = { type: 'category', data: rows.map(row => row.label), axisLabel: { rotate: 35 } };
      option.yAxis = { type: 'value', name: 'mV' };
      option.series = [
        { name: '量測 A', type: 'bar', itemStyle: { color: palette[0] }, data: rows.map(row => row.first) },
        { name: '量測 B', type: 'bar', itemStyle: { color: palette[1] }, data: rows.map(row => row.second) },
      ];
      chart.setOption(option);
      window.addEventListener('resize', () => chart.resize(), { passive: true });
    },
  };
})();
