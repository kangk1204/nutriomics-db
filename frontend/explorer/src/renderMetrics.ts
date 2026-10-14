import type { GraphData } from './types';

// Per-response monotonic timing; never put browser clock values in scientific exports.
const receipts = new WeakMap<GraphData, number>();
export function recordGraphReceipt(graph: GraphData, at = performance.now()): GraphData {
  receipts.set(graph, at);
  return graph;
}
export function graphTiming(graph: GraphData, started: number, frame: number, layout: number, displayed: number) {
  const received = receipts.get(graph);
  return {
    layout_ms: layout,
    render_to_frame_ms: frame - started,
    data_to_frame_ms: received === undefined ? null : frame - received,
    preloaded_before_render_ms: received === undefined ? null : Math.max(0, started - received),
    displayed_nodes: displayed,
    total_nodes: graph.nodes.length,
  };
}
