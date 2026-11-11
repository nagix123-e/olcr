import "./banter-loader.css";

export function BanterLoader({ariaLabel="Generating response"}:{ariaLabel?:string}){
  return <div className="loading-container composer-plan-loader" role="status" aria-label={ariaLabel}>
    <div className="loader">
      {Array.from({length:4},(_,index)=><span className="cube" key={index}/>) }
    </div>
  </div>;
}
