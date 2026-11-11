import {cleanup, fireEvent, render, screen} from '@testing-library/react';
import {afterEach, expect, it, vi} from 'vitest';
import {ComposerActions} from './ComposerActions';
afterEach(cleanup);
it('offers one attachment flow for files and working folders', () => {
  const onFolder = vi.fn(), onFile = vi.fn(), onDiagram = vi.fn();
  render(<ComposerActions disabled={false} onFolder={onFolder} onFile={onFile} onDiagram={onDiagram}/>);
  fireEvent.click(screen.getByRole('button', {name:/操作を追加|Add action/}));
  fireEvent.click(screen.getByRole('menuitem', {name:/ファイル・フォルダを添付|Attach files or a folder/}));
  expect(onFolder).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('menuitem', {name:/フォルダを選ぶ|Choose a folder/}));
  expect(onFolder).toHaveBeenCalledOnce();
  expect(onFile).not.toHaveBeenCalled();
  expect(onDiagram).not.toHaveBeenCalled();
  expect(screen.queryByRole('menu')).toBeNull();
});


it('uses a distinct map and pin icon for place selection',()=>{
  render(<ComposerActions disabled={false} onFile={vi.fn()} onDiagram={vi.fn()} onMap={vi.fn()}/>);
  fireEvent.click(screen.getByRole('button',{name:/操作を追加|Add action/}));
  const mapIcon=screen.getByRole('menuitem',{name:/地図・地点を指定|Map \/ select places/}).querySelector('svg');
  const diagramIcon=screen.getByRole('menuitem',{name:/概念図|Concept diagram/}).querySelector('svg');
  expect(mapIcon?.innerHTML).toContain('M18 20');
  expect(mapIcon?.innerHTML).not.toEqual(diagramIcon?.innerHTML);
});
