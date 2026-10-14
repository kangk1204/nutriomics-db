import type { GraphData, GraphPath, Measurement } from './types';

/** Reading order only; do not combine analytical methods or change values/units. */
export function nutrientReadingPriority(label: string): number {
  const name = label.toLocaleLowerCase();
  const groups = [/^(energy|에너지|열량)/, /^(protein|단백질)/, /^(total lipid|total fat|지방$|총지방)/, /^(carbohydrate|탄수화물)/, /^(fiber|dietary fiber|식이섬유)/, /^(fatty acids|지방산)/, /^(cholesterol|콜레스테롤)/, /^(sodium|나트륨)/];
  const index = groups.findIndex(pattern => pattern.test(name));
  return index < 0 ? groups.length : index;
}
export function measurementsForReading(rows: Measurement[]): Measurement[] {
  return [...rows].sort((a, b) => nutrientReadingPriority(a.component_label || a.component || '') - nutrientReadingPriority(b.component_label || b.component || ''));
}

/** Presentation subset only: never manufacture a node, relationship, or path. */
export function graphForDisplay(graph: GraphData, seedId: string, mode: 'focus' | 'all', selectedPath?: GraphPath): GraphData {
  if (mode === 'all') return graph;
  const chosenPath = selectedPath || graph.paths[0];
  if (chosenPath?.node_ids.length) {
    const ids = new Set(chosenPath.node_ids.slice(0, 12));
    const pairs = new Set(chosenPath.edge_ids.map(id => graph.edges.find(edge => edge.id === id)).filter(Boolean).map(edge => `${edge!.subject}|${edge!.object}`));
    return { ...graph, nodes: graph.nodes.filter(node => ids.has(node.id)), edges: graph.edges.filter(edge => ids.has(edge.subject) && ids.has(edge.object) && pairs.has(`${edge.subject}|${edge.object}`)) };
  }
  const actualSeed = graph.nodes.some(node => node.id === seedId) ? seedId : graph.nodes[0]?.id;
  const ids = new Set(actualSeed ? [actualSeed] : []);
  const labelById = new Map(graph.nodes.map(node => [node.id, node.label]));
  const adjacent = [...graph.edges.filter(edge => edge.subject === actualSeed), ...graph.edges.filter(edge => edge.object === actualSeed)]
    .sort((a, b) => nutrientReadingPriority(labelById.get(a.subject === actualSeed ? a.object : a.subject) || '') - nutrientReadingPriority(labelById.get(b.subject === actualSeed ? b.object : b.subject) || ''));
  for (const edge of adjacent) {
    if (ids.size >= 5) break;
    ids.add(edge.subject); ids.add(edge.object);
  }
  return { ...graph, nodes: graph.nodes.filter(node => ids.has(node.id)), edges: graph.edges.filter(edge => ids.has(edge.subject) && ids.has(edge.object) && (edge.subject === actualSeed || edge.object === actualSeed)) };
}

export function weakestLinkLabel(path: GraphPath, graph?: GraphData | null): string {
  const edges = path.edge_ids.map(id => graph?.edges.find(edge => edge.id === id)).filter(Boolean);
  if (edges.some(edge => edge?.relation === 'candidate_name_correspondence')) return '가장 약한 구간: 후보 이름 대응';
  if (edges.some(edge => edge?.tier === 'extracted')) return '가장 약한 구간: 자동 문헌 추출 후보';
  if (edges.some(edge => ['candidate', 'inferred', 'predicted', 'regulatory_identity'].includes(edge?.tier || ''))) return '가장 약한 구간: 간접·후보 연결';
  if (edges.some(edge => edge?.tier === 'restricted')) return '상세 이용조건이 미확인인 연결 포함';
  return '서로 다른 출처의 조건을 함께 확인해야 하는 경로';
}
