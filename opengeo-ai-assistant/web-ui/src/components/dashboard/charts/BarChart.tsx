import React from 'react';

interface BarChartProps {
  values: number[];
  labels: string[];
  title: string;
  emptyText: string;
  /** Show every n-th axis label so dense charts stay legible. */
  labelEvery?: number;
}

const WIDTH = 240;
const HEIGHT = 72;
const AXIS = 14;
const GAP = 2;

/** Small inline-SVG bar chart; each bar carries its label and value for screen readers. */
const BarChart: React.FC<BarChartProps> = ({
  values,
  labels,
  title,
  emptyText,
  labelEvery = 1,
}) => {
  const max = Math.max(0, ...values);
  if (max === 0) {
    return <p className="dashboard-chart-empty">{emptyText}</p>;
  }
  const barWidth = (WIDTH - GAP * (values.length - 1)) / values.length;
  const plotHeight = HEIGHT - AXIS;

  return (
    <figure className="dashboard-chart">
      <figcaption>{title}</figcaption>
      <svg viewBox={`0 0 ${WIDTH} ${HEIGHT}`} role="img" aria-label={title}>
        {values.map((value, index) => {
          const height = (value / max) * plotHeight;
          const x = index * (barWidth + GAP);
          return (
            <g key={labels[index]}>
              <rect
                className="dashboard-chart-bar"
                x={x}
                y={plotHeight - height}
                width={barWidth}
                height={Math.max(height, value > 0 ? 1 : 0)}
                aria-label={`${labels[index]}: ${value}`}
              >
                <title>{`${labels[index]}: ${value}`}</title>
              </rect>
              {index % labelEvery === 0 && (
                <text
                  className="dashboard-chart-label"
                  x={x + barWidth / 2}
                  y={HEIGHT - 2}
                  textAnchor="middle"
                >
                  {labels[index]}
                </text>
              )}
            </g>
          );
        })}
      </svg>
    </figure>
  );
};

export default BarChart;
