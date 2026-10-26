import React from 'react';
import { describe, it, expect, vi } from 'vitest';
import { fireEvent, render } from '@testing-library/react';
import ResizablePanel from '../ResizablePanel';

function drag(fromX: number, toX: number, onWidthChange = vi.fn()) {
  const { container } = render(
    <ResizablePanel defaultWidth={340} minWidth={280} maxWidth={560} onWidthChange={onWidthChange}>
      <p>content</p>
    </ResizablePanel>
  );
  fireEvent.mouseDown(container.querySelector('.resize-handle')!, { clientX: fromX });
  fireEvent.mouseMove(document, { clientX: toX });
  fireEvent.mouseUp(document);
  return { panel: container.firstChild as HTMLElement, onWidthChange };
}

describe('ResizablePanel', () => {
  it('widens when its left-edge handle is dragged left', () => {
    const { panel, onWidthChange } = drag(500, 400);
    expect(panel.style.width).toBe('440px');
    expect(onWidthChange).toHaveBeenLastCalledWith(440);
  });

  it('clamps to maxWidth and minWidth', () => {
    expect(drag(500, 0).panel.style.width).toBe('560px');
    expect(drag(500, 900).panel.style.width).toBe('280px');
  });

  it('stops resizing after mouseup', () => {
    const { panel } = drag(500, 400);
    fireEvent.mouseMove(document, { clientX: 0 });
    expect(panel.style.width).toBe('440px');
  });
});
