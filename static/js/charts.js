/* Minimal dependency-free SVG charts for Atten statistics.
   Single-hue marks (magnitude), recessive grid, rounded data-ends,
   2px gaps between bars, hover tooltips. */
(function () {
  var NS = 'http://www.w3.org/2000/svg';

  function el(tag, attrs) {
    var n = document.createElementNS(NS, tag);
    for (var k in attrs) n.setAttribute(k, attrs[k]);
    return n;
  }

  // Bar path with rounded ends on the data side only, anchored to baseline.
  function vBarPath(x, y, w, h, r) {
    r = Math.max(0, Math.min(r, w / 2, h));
    if (h <= 0) return '';
    return 'M' + x + ',' + (y + h) + 'L' + x + ',' + (y + r) +
           'Q' + x + ',' + y + ' ' + (x + r) + ',' + y +
           'L' + (x + w - r) + ',' + y +
           'Q' + (x + w) + ',' + y + ' ' + (x + w) + ',' + (y + r) +
           'L' + (x + w) + ',' + (y + h) + 'Z';
  }
  function hBarPath(x, y, w, h, r) {
    r = Math.max(0, Math.min(r, h / 2, w));
    if (w <= 0) return '';
    return 'M' + x + ',' + y + 'L' + (x + w - r) + ',' + y +
           'Q' + (x + w) + ',' + y + ' ' + (x + w) + ',' + (y + r) +
           'L' + (x + w) + ',' + (y + h - r) +
           'Q' + (x + w) + ',' + (y + h) + ' ' + (x + w - r) + ',' + (y + h) +
           'L' + x + ',' + (y + h) + 'Z';
  }

  function niceMax(v) {
    if (v <= 0) return 1;
    var pow = Math.pow(10, Math.floor(Math.log10(v)));
    var n = v / pow;
    var step = n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10;
    return step * pow;
  }

  function tooltip(container) {
    var tip = container.querySelector('.chart-tip');
    if (!tip) {
      tip = document.createElement('div');
      tip.className = 'chart-tip';
      container.appendChild(tip);
    }
    return tip;
  }

  function bindTip(container, hit, bar, html) {
    var tip = tooltip(container);
    hit.addEventListener('mouseenter', function () {
      tip.innerHTML = html;
      tip.classList.add('show');
      if (bar) bar.classList.add('hot');
    });
    hit.addEventListener('mousemove', function (e) {
      var r = container.getBoundingClientRect();
      tip.style.left = (e.clientX - r.left) + 'px';
      tip.style.top = (e.clientY - r.top - 6) + 'px';
    });
    hit.addEventListener('mouseleave', function () {
      tip.classList.remove('show');
      if (bar) bar.classList.remove('hot');
    });
  }

  /* ---- Vertical bar chart (time series / distributions) ---- */
  function vbar(container, data, opts) {
    opts = opts || {};
    var W = container.clientWidth || 600, H = opts.height || 200;
    var padL = 30, padR = 6, padT = 10, padB = 22;
    var plotW = W - padL - padR, plotH = H - padT - padB;
    container.querySelectorAll('svg').forEach(function (s) { s.remove(); });
    if (!data.length) return;

    var svg = el('svg', { viewBox: '0 0 ' + W + ' ' + H, height: H, role: 'img' });
    var max = niceMax(Math.max.apply(null, data.map(function (d) { return d.value; })));

    // gridlines + y labels (recessive)
    for (var i = 0; i <= 2; i++) {
      var val = max * i / 2, y = padT + plotH - (val / max) * plotH;
      svg.appendChild(el('line', { class: 'grid-line', x1: padL, x2: W - padR, y1: y, y2: y }));
      var t = el('text', { class: 'axis-text', x: padL - 6, y: y + 3, 'text-anchor': 'end' });
      t.textContent = Math.round(val * 10) / 10;
      svg.appendChild(t);
    }

    var slot = plotW / data.length;
    var bw = Math.max(1, slot - 2);            // 2px surface gap between bars
    var every = Math.ceil(data.length / (opts.maxLabels || 8));

    data.forEach(function (d, i) {
      var h = max ? (d.value / max) * plotH : 0;
      var x = padL + i * slot + 1, y = padT + plotH - h;
      var bar = el('path', { class: 'bar', d: vBarPath(x, y, bw, h, 4) });
      svg.appendChild(bar);
      var hit = el('rect', { class: 'bar-hit', x: padL + i * slot, y: padT, width: slot, height: plotH });
      svg.appendChild(hit);
      bindTip(container, hit, bar,
        '<b>' + d.value + '</b> ' + (opts.unit || '') + '<br>' + (d.sub ? d.sub + ' · ' : '') + d.label);
      if (i % every === 0) {
        var lab = el('text', { class: 'axis-text', x: x + bw / 2, y: H - 6, 'text-anchor': 'middle' });
        lab.textContent = d.label;
        svg.appendChild(lab);
      }
    });
    container.insertBefore(svg, container.firstChild);
  }

  /* ---- Horizontal bar chart (categories with long labels) ---- */
  function hbar(container, data, opts) {
    opts = opts || {};
    var W = container.clientWidth || 600;
    var rowH = opts.rowH || 30, gap = 2;
    var H = data.length * rowH;
    var labelW = Math.min(opts.labelW || 130, Math.max(90, W * 0.36));
    var valW = 38, padR = 4;
    var plotW = W - labelW - valW - padR;
    container.querySelectorAll('svg').forEach(function (s) { s.remove(); });
    if (!data.length) return;

    var svg = el('svg', { viewBox: '0 0 ' + W + ' ' + H, height: H, role: 'img' });
    var max = Math.max.apply(null, data.map(function (d) { return d.value; })) || 1;

    data.forEach(function (d, i) {
      var y = i * rowH, bh = rowH - gap * 2 - 6;
      var w = Math.max(d.value > 0 ? 2 : 0, (d.value / max) * plotW);

      var lab = el('text', { class: 'axis-text', x: 0, y: y + rowH / 2 + 4 });
      lab.textContent = d.label.length > 22 ? d.label.slice(0, 21) + '…' : d.label;
      svg.appendChild(lab);

      // track
      svg.appendChild(el('rect', {
        x: labelW, y: y + (rowH - bh) / 2, width: plotW, height: bh,
        rx: 5, fill: '#eef0f6'
      }));
      var bar = el('path', { class: 'bar', d: hBarPath(labelW, y + (rowH - bh) / 2, w, bh, 4) });
      svg.appendChild(bar);

      var v = el('text', { class: 'bar-label', x: labelW + plotW + 6, y: y + rowH / 2 + 4 });
      v.textContent = d.value;
      svg.appendChild(v);

      var hit = el('rect', { class: 'bar-hit', x: 0, y: y, width: W, height: rowH });
      svg.appendChild(hit);
      bindTip(container, hit, bar,
        '<b>' + d.label + '</b><br>' + d.value + ' ' + (opts.unit || '') + (d.sub ? '<br>' + d.sub : ''));
    });
    container.insertBefore(svg, container.firstChild);
  }

  function renderAll() {
    document.querySelectorAll('[data-chart]').forEach(function (c) {
      var data;
      try { data = JSON.parse(c.getAttribute('data-values')); } catch (e) { return; }
      var opts = {
        unit: c.getAttribute('data-unit') || '',
        height: parseInt(c.getAttribute('data-height'), 10) || 200,
        maxLabels: parseInt(c.getAttribute('data-max-labels'), 10) || 8
      };
      if (c.getAttribute('data-chart') === 'hbar') hbar(c, data, opts);
      else vbar(c, data, opts);
    });
  }

  function hideTips() {
    document.querySelectorAll('.chart-tip.show').forEach(function (t) {
      t.classList.remove('show');
    });
    document.querySelectorAll('.bar.hot').forEach(function (b) {
      b.classList.remove('hot');
    });
  }

  window.addEventListener('load', renderAll);
  // Scrolling moves marks out from under the cursor without firing mouseleave,
  // which would otherwise strand a tooltip over the axis labels.
  window.addEventListener('scroll', hideTips, { passive: true });
  var rt;
  window.addEventListener('resize', function () {
    hideTips();
    clearTimeout(rt); rt = setTimeout(renderAll, 150);
  });
})();
