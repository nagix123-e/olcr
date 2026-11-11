import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react';
import {afterEach,expect,it,vi} from 'vitest';
const openExternal=vi.hoisted(()=>vi.fn().mockResolvedValue(undefined));
vi.mock('./native',()=>({openExternal}));
vi.mock('./language',()=>({useLanguage:()=>({language:'en'})}));
import {ServicePurchaseCard,type ServicePurchaseBlock} from './ServicePurchaseCard';
afterEach(()=>{cleanup();vi.clearAllMocks()});
const block:ServicePurchaseBlock={type:'service_purchase',id:'review-1',status:'pending',order:{merchant:'Example Shop',url:'https://example.com/checkout',items:[{name:'Notebook',quantity:2,unit_price:'10.00'}],subtotal:'20.00',shipping:'2.00',tax:'1.50',total:'23.50',currency:'USD'}};
it('shows exact merchant and amounts and opens checkout only on the user click after confirmation',async()=>{
 const api=vi.fn().mockResolvedValue({status:'checkout_ready',url:block.order.url,purchased:false});
 render(<ServicePurchaseCard block={block} api={api}/>);
 expect(screen.getByText('Example Shop')).toBeInTheDocument();expect(screen.getByText('USD 23.50')).toBeInTheDocument();
 fireEvent.click(screen.getByRole('button',{name:'Confirm order details'}));
 await waitFor(()=>expect(api).toHaveBeenCalledWith('/connectors/purchases/review-1/confirm',{method:'POST',body:JSON.stringify({approve:true,order:block.order})}));
 expect(openExternal).not.toHaveBeenCalled();
 expect(screen.getByText('Not purchased. Complete payment on the merchant website.')).toBeInTheDocument();
 fireEvent.click(screen.getByRole('button',{name:'Open merchant checkout'}));
 await waitFor(()=>expect(openExternal).toHaveBeenCalledWith(block.order.url));
});
it('sends denial without opening the checkout',async()=>{
 const api=vi.fn().mockResolvedValue({status:'denied',purchased:false});render(<ServicePurchaseCard block={block} api={api}/>);
 fireEvent.click(screen.getByRole('button',{name:'Cancel purchase'}));
 await waitFor(()=>expect(api).toHaveBeenCalledWith('/connectors/purchases/review-1/confirm',{method:'POST',body:JSON.stringify({approve:false,order:block.order})}));
 expect(await screen.findByText('Purchase cancelled.')).toBeInTheDocument();expect(openExternal).not.toHaveBeenCalled();
});
it('shows an expired review without implying a purchase or opening checkout',async()=>{
 const api=vi.fn().mockRejectedValue(Error('PURCHASE_APPROVAL_EXPIRED'));render(<ServicePurchaseCard block={block} api={api}/>);
 fireEvent.click(screen.getByRole('button',{name:'Confirm order details'}));
 expect(await screen.findByRole('alert')).toHaveTextContent('This review has expired. Prepare the order again.');
 expect(screen.queryByRole('button',{name:'Open merchant checkout'})).not.toBeInTheDocument();expect(openExternal).not.toHaveBeenCalled();
});
it('rejects a checkout destination changed after review',async()=>{
 const api=vi.fn().mockResolvedValue({status:'checkout_ready',url:'https://other.example/checkout',purchased:false});
 render(<ServicePurchaseCard block={block} api={api}/>);fireEvent.click(screen.getByRole('button',{name:'Confirm order details'}));
 expect(await screen.findByRole('alert')).toHaveTextContent('PURCHASE_CHECKOUT_URL_CHANGED');
 expect(screen.queryByRole('button',{name:'Open merchant checkout'})).not.toBeInTheDocument();expect(openExternal).not.toHaveBeenCalled();
});
it('labels estimated totals and unknown shipping and tax without claiming final cost or payment',()=>{
 render(<ServicePurchaseCard block={{...block,order:{...block.order,estimated:true,shipping:null,tax:null}}} api={vi.fn()}/>);
 expect(screen.getByText('Estimated total')).toBeInTheDocument();
 expect(screen.getAllByText('Determined at checkout')).toHaveLength(2);
 expect(screen.getByText('Confirm the final amount, including shipping and tax, on the merchant website.')).toBeInTheDocument();
 expect(screen.getByText('Not purchased. Complete payment on the merchant website.')).toBeInTheDocument();
});
