/* Chart theming helpers + the working themechange subscription.
 *
 * The old pages registered `window.addEventListener('themechange', …)`, but
 * app.js dispatches the event on `document` with bubbles:false, so those
 * listeners never fired and charts kept stale colours after a Red/Blue theme
 * switch until reload. Register through onThemeChange() and it works. */

function cssVar(name, fallback = '') {
  const value = getComputedStyle(document.documentElement)
    .getPropertyValue(name).trim();
  return value || fallback;
}

/* Palette for Chart.js datasets, resolved from the active theme's CSS vars. */
function chartThemeColors() {
  return {
    pos: cssVar('--pos', '#22c55e'),
    neg: cssVar('--neg', '#ef4444'),
    accent: cssVar('--accent', '#3b82f6'),
    grid: cssVar('--chart-grid', 'rgba(128,128,128,.2)'),
    text: cssVar('--text-dim', '#9aa4b2'),
  };
}

/* Subscribe to theme switches. Returns an unsubscribe function. */
function onThemeChange(handler) {
  const listener = event => handler(event.detail?.theme, chartThemeColors());
  document.addEventListener('themechange', listener);
  return () => document.removeEventListener('themechange', listener);
}

/* Re-colour an existing Chart.js instance in place after a theme switch.
 * colorize(chart, colors) applies dataset-specific colours; grid/tick colours
 * are handled generically here. */
function rethemeChart(chart, colorize) {
  if (!chart) return;
  const colors = chartThemeColors();
  const scales = chart.options?.scales || {};
  Object.values(scales).forEach(scale => {
    if (scale.grid) scale.grid.color = colors.grid;
    if (scale.ticks) scale.ticks.color = colors.text;
  });
  if (typeof colorize === 'function') colorize(chart, colors);
  chart.update('none');
}
