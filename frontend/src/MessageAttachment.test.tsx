import {act,cleanup,render,screen} from '@testing-library/react';
import {afterEach,expect,it,vi} from 'vitest';
import {MessageAttachment} from './MessageAttachment';
const request=vi.hoisted(()=>vi.fn());
vi.mock('./api',()=>({api:request}));
afterEach(()=>{cleanup();vi.useRealTimers();vi.resetAllMocks()});
it('shows the sent image until exactly 24 hours then only its filename',async()=>{
 vi.useFakeTimers();vi.setSystemTime(100000);
 render(<MessageAttachment value={{name:'photo.png',preview_data_url:'data:image/png;base64,aA==',preview_expires_at:86500}}/>);
 expect(screen.getByRole('img')).toHaveAttribute('alt','photo.png');
 await act(async()=>{vi.advanceTimersByTime(86399999)});expect(screen.getByRole('img')).toBeInTheDocument();
 await act(async()=>{vi.advanceTimersByTime(1)});expect(screen.queryByRole('img')).toBeNull();expect(screen.getByText('photo.png')).toBeInTheDocument();
});
it('restores a saved preview when reopening a conversation',async()=>{
 request.mockResolvedValue({data_url:'data:image/png;base64,aA==',expires_at:Date.now()/1000+3600});
 render(<MessageAttachment value={{name:'photo.png',preview_id:'saved',preview_expires_at:Date.now()/1000+3600}}/>);
 expect(await screen.findByRole('img')).toBeInTheDocument();expect(request).toHaveBeenCalledWith('/message-image-previews/saved');
});
it('keeps expired and unavailable images as filenames',async()=>{
 render(<MessageAttachment value={{name:'old.png',preview_id:'old',preview_expires_at:1}}/>);
 expect(request).not.toHaveBeenCalled();expect(screen.queryByRole('img')).toBeNull();expect(screen.getByText('old.png')).toBeInTheDocument();
});
