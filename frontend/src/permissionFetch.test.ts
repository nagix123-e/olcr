import {afterEach,describe,expect,it,vi} from 'vitest';
import {configureApi,permissionFetch} from './api';
afterEach(()=>{vi.unstubAllGlobals();vi.restoreAllMocks()});
const challenge=()=>new Response(JSON.stringify({detail:{code:'PERMISSION_CONFIRMATION_REQUIRED',approval_id:'grant-1',summary:'Allow?'}}),{status:409});
describe('permission transport',()=>{
 it('approval retries the identical request once with its grant',async()=>{
  configureApi('http://localhost:9','test');
  const fetch=vi.fn().mockResolvedValueOnce(challenge()).mockResolvedValueOnce(new Response('{}')).mockResolvedValueOnce(new Response('done'));
  vi.stubGlobal('fetch',fetch);vi.spyOn(window,'confirm').mockReturnValue(true);
  const init={method:'POST',headers:{'Content-Type':'application/json','X-OLCR-Session':'test'},body:'{"message":"run"}'};
  expect(await (await permissionFetch('http://localhost:9/chat',init)).text()).toBe('done');
  expect(fetch.mock.calls[2][1].body).toBe(init.body);
  expect(fetch.mock.calls[2][1].headers.get('X-OLCR-Approval')).toBe('grant-1');
  expect(fetch.mock.calls[2][1].headers.get('X-OLCR-Session')).toBe('test');
 });
 it('decline records rejection without retrying execution',async()=>{
  const fetch=vi.fn().mockResolvedValueOnce(challenge()).mockResolvedValueOnce(new Response('{}'));
  vi.stubGlobal('fetch',fetch);vi.spyOn(window,'confirm').mockReturnValue(false);
  await expect(permissionFetch('/chat',{method:'POST',body:'{}'})).rejects.toThrow();
  expect(fetch).toHaveBeenCalledTimes(2);
  expect(fetch.mock.calls[1][1].body).toBe('{"approve":false}');
 });
 it('automatic and full responses need no extra request or dialog',async()=>{
  const fetch=vi.fn().mockResolvedValue(new Response('{}'));
  vi.stubGlobal('fetch',fetch);const confirm=vi.spyOn(window,'confirm');
  await permissionFetch('/chat',{method:'POST',body:'{}'});
  expect(fetch).toHaveBeenCalledTimes(1);expect(confirm).not.toHaveBeenCalled();
 });
});
