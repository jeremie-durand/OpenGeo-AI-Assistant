import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import BarChart from '../charts/BarChart';

const MONTHS = ['N', 'D', 'J', 'F', 'M', 'A', 'M2', 'J2', 'J3', 'A2', 'S', 'O'];

describe('BarChart', () => {
  it('draws one labelled bar per value', () => {
    const values = [1, 0, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12];
    const { container } = render(
      <BarChart values={values} labels={MONTHS} title="Scenes per month" emptyText="None" />
    );
    expect(container.querySelectorAll('rect')).toHaveLength(12);
    expect(screen.getByRole('img', { name: 'Scenes per month' })).toBeInTheDocument();
    expect(screen.getByLabelText('M: 5')).toBeInTheDocument();
  });

  it('shows the empty state when every value is zero', () => {
    const { container } = render(
      <BarChart values={[0, 0, 0]} labels={['a', 'b', 'c']} title="T" emptyText="No scenes" />
    );
    expect(screen.getByText('No scenes')).toBeInTheDocument();
    expect(container.querySelector('svg')).toBeNull();
  });
});
