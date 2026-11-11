import {describe,it,expect} from 'vitest';
import {collectVisibleDomObservation,locateVisibleDomElement} from './screenObservation';

describe('DOM-only screen observation',()=>{
 it('collects visible semantic labels without values, hidden content, or geometry',()=>{
  document.body.innerHTML='<main><header><span class="ready">READY</span></header><h1>Workspace</h1><button>Save</button><button disabled>Delete</button><input aria-label="Secret" value="do-not-send"><div hidden><button>Hidden text</button></div><details><summary>Closed section</summary><button>Hidden details action</button></details></main>';
  const state=collectVisibleDomObservation();
  expect(state.dom.elements.map(item=>item.label)).toEqual(['READY','Workspace','Save','Delete','Closed section']);
  expect(state.dom.elements[2].enabled).toBe(true);
  expect(state.dom.elements[3].enabled).toBe(false);
  expect(state.dom.elements[1].level).toBe(1);
  expect(JSON.stringify(state)).not.toContain('do-not-send');
  expect(JSON.stringify(state)).not.toContain('Hidden text');
  expect(JSON.stringify(state)).not.toContain('Hidden details action');
  expect(JSON.stringify(state)).not.toMatch(/bbox|coordinates|"x"|"y"/);
  expect(state.dom.elements[4].expanded).toBe(false);
  expect(locateVisibleDomElement(state.dom.elements[4].id)?.tagName).toBe('SUMMARY');
 });
});
