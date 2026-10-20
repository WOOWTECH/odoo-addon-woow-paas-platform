/** @odoo-module **/

import { Component } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";

const COLORS = ["#6183fc", "#27a88a", "#f0a43c", "#9a6ce0", "#e36e81", "#4d9ecb", "#82984d", "#bf7b45"];
const CHART_HEIGHT = 156;
const TOP = 8;
const BOTTOM = 26;
const STEP = 42;
const BAR = 22;

/** Build stack rectangles using a shared max so each date and series is comparable. */
export function usageChartLayout(data) {
  const dates = Array.isArray(data?.dates) ? data.dates : [];
  const series = Array.isArray(data?.series) ? data.series : [];
  const max = Math.max(1, ...dates.map((_, i) => series.reduce((sum, item) => {
    const value = Number(item.values?.[i]);
    return sum + (Number.isFinite(value) && value > 0 ? value : 0);
  }, 0)));
  const plotHeight = CHART_HEIGHT - TOP - BOTTOM;
  const bars = dates.map((date, dateIndex) => {
    let used = 0;
    const segments = series.map((item, seriesIndex) => {
      const raw = Number(item.values?.[dateIndex]);
      const value = Number.isFinite(raw) && raw > 0 ? raw : 0;
      const height = value ? (value / max) * plotHeight : 0;
      const segment = {
        key: `${date}-${item.id}`,
        label: item.label || _t("Unknown key"),
        value,
        x: dateIndex * STEP + (STEP - BAR) / 2,
        y: TOP + plotHeight - used - height,
        width: BAR,
        height,
        color: COLORS[seriesIndex % COLORS.length],
      };
      used += height;
      return segment;
    }).filter((segment) => segment.height > 0);
    return { date, x: dateIndex * STEP + STEP / 2, segments };
  });
  return {
    width: Math.max(STEP, dates.length * STEP),
    height: CHART_HEIGHT,
    plotHeight,
    bars,
    series: series.map((item, index) => ({ ...item, color: COLORS[index % COLORS.length] })),
    metric: data?.metric || "spend",
    max,
  };
}

export class LlmUsageChart extends Component {
  static template = "woow_paas_platform.LlmUsageChart";
  static props = {
    data: { type: Object, optional: true },
    narrow: { type: Boolean, optional: true },
    title: { type: String, optional: true },
    formatValue: { type: Function, optional: true },
  };

  get layout() { return usageChartLayout(this.props.data); }
  get tableRows() {
    const dates = this.layout.bars.map((bar) => bar.date);
    return dates.map((date) => {
      const index = this.props.data?.dates?.indexOf(date) ?? -1;
      return {
        date,
        values: this.layout.series.map((item) => {
          const value = Number(item.values?.[index]);
          return this.format(Number.isFinite(value) ? value : 0);
        }),
      };
    });
  }
  get unknownLabel() { return _t("Unknown key"); }
  get chartTitle() { return this.props.title || _t("Daily usage"); }
  get description() {
    return _t("Stacked daily %(metric)s by %(group)s", {
      metric: this.layout.metric,
      group: this.props.data?.group_by || "model",
    });
  }
  format(value) { return this.props.formatValue ? this.props.formatValue(value) : String(value); }
  dateLabel(date) { return date.slice(5); }
}
