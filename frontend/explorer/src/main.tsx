import React from 'react';
import { createRoot } from 'react-dom/client';
import App from './App';
import Workbench from './Workbench';
import LiteratureWorkbench from './LiteratureWorkbench';
import './styles.css';
function Root() {
  const [literature,setLiterature]=React.useState(new URLSearchParams(location.search).get('view')==='literature');
  const [legacy,setLegacy]=React.useState(new URLSearchParams(location.search).get('view')==='legacy');
  if(literature&&!window.__NUTRIOMICS_SNAPSHOT__)return <LiteratureWorkbench onBack={()=>{history.replaceState(null,'',location.pathname);setLiterature(false)}}/>;
  return <>{legacy&&!window.__NUTRIOMICS_SNAPSHOT__&&<button style={{position:'fixed',right:20,bottom:15,zIndex:999,border:'1px solid #bbcec2',borderRadius:8,background:'#fff',padding:'10px 15px',color:'#185a45',fontSize:13,boxShadow:'0 3px 15px #203d3020'}} onClick={()=>{history.replaceState(null,'',location.pathname+'?view=literature');setLiterature(true)}}>문헌·모델 평가</button>}{legacy?<><button style={{position:'fixed',right:160,bottom:15,zIndex:999}} onClick={()=>setLegacy(false)}>근거 워크벤치</button><App/></>:<Workbench onLegacy={()=>setLegacy(true)} onLiterature={window.__NUTRIOMICS_SNAPSHOT__?undefined:()=>{history.replaceState(null,'',location.pathname+'?view=literature');setLiterature(true)}}/>}</>;
}
createRoot(document.getElementById('root')!).render(<React.StrictMode><Root /></React.StrictMode>);
