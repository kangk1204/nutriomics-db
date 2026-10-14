import React from 'react';
import { createRoot } from 'react-dom/client';
import App from './App';
import Workbench from './Workbench';
import './styles.css';
function Root() {
  const [legacy,setLegacy]=React.useState(new URLSearchParams(location.search).get('view')==='legacy');
  return legacy?<><button style={{position:'fixed',right:20,bottom:15,zIndex:999}} onClick={()=>setLegacy(false)}>근거 워크벤치</button><App/></>:<Workbench onLegacy={()=>setLegacy(true)}/>;
}
createRoot(document.getElementById('root')!).render(<React.StrictMode><Root /></React.StrictMode>);
