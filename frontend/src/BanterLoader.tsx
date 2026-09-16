import "./banter-loader.css";

export function BanterLoader(){
  return <div className="loading-container" role="status" aria-label="Generating response">
    <div className="loader">
      {Array.from({length:4},(_,index)=><span className="cube" key={index}/>) }
    </div>
  </div>;
}
