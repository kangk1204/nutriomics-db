import type { Entity } from './types';

export function geneNameKey(value:string) {
  return value.normalize('NFKC').toLocaleLowerCase()
    .replace(/[αβγδκ]/g,v=>({α:'alpha',β:'beta',γ:'gamma',δ:'delta',κ:'kappa'}[v]||v))
    .replace(/[^\p{L}\p{N}]/gu,'');
}

export function geneNameMatches(node:Entity,query:string) {
  if(node.kind!=='gene'||!geneNameKey(query))return false;
  return Boolean(node.gene_names?.terms.some(term=>geneNameKey(term.name).includes(geneNameKey(query))));
}

export function geneNameRank(node:Entity,query:string) {
  if(node.kind!=='gene'||!query.trim())return 2;
  const terms=node.gene_names?.terms||[{name:node.label,kind:'approved_symbol'},{name:node.id,kind:'identifier'}];
  const exact=terms.filter(term=>geneNameKey(term.name)===geneNameKey(query));
  return exact.some(term=>['approved_symbol','approved_name','identifier','authored_korean_name'].includes(term.kind))?0:exact.length?1:2;
}
