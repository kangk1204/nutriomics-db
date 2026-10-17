"""Lazy batched graph hydration; preserve the original bounded traversal exactly."""
from __future__ import annotations

from .explorer_performance import has_index


class GraphReads:
    def __init__(self, db, decode_node, decode_edge, domain, tiers, include_extracted):
        self.db, self.decode_node, self.decode_edge = db, decode_node, decode_edge
        self.domain, self.tiers, self.include_extracted = domain, tiers, include_extracted
        self.indexed = has_index(db, "perf_adj")
        self.covering = self.indexed and bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='perf_adj_object_cover'").fetchone())
        self.traversal_covering = self.indexed and bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='perf_adj_object_traversal_cover'").fetchone())
        self.nodes, self.edges = {}, {}

    def load_nodes(self, identifiers):
        missing = list(set(identifiers) - self.nodes.keys())
        for start in range(0, len(missing), 500):
            ids = missing[start:start+500]
            for row in self.db.execute("SELECT * FROM node WHERE id IN (" + ",".join("?" for _ in ids) + ")", ids):
                self.nodes[row["id"]] = dict(row)

    def node(self, identifier):
        self.load_nodes([identifier])
        return self.nodes.get(identifier)

    def load_edges(self, identifiers):
        missing = list(dict.fromkeys(i for i in identifiers if i not in self.edges))
        for start in range(0, len(missing), 500):
            ids = missing[start:start+500]
            rows = list(self.db.execute("SELECT * FROM edge WHERE id IN (" + ",".join("?" for _ in ids) + ")", ids))
            items = [self.decode_edge(r) for r in rows]
            mids = list({e["context"].get("measurement_id") for e in items} - {None})
            measurements = {r["id"]: {k: r[k] for k in ("amount", "unit", "basis", "status")} for r in self.db.execute("SELECT id,amount,unit,basis,status FROM measurement WHERE id IN (" + ",".join("?" for _ in mids) + ")", mids)} if mids else {}
            for edge in items:
                m = measurements.get(edge["context"].get("measurement_id"))
                if m is not None:
                    edge["context"]["measurement"] = m
                self.edges[edge["id"]] = edge
            self.load_nodes([v for e in items for v in (e["subject"], e["object"])])
        return [self.edges[i] for i in identifiers if i in self.edges]

    def clause(self):
        clauses, args = [], []
        if self.domain != "all":
            clauses.append("EXISTS(SELECT 1 FROM edge_domain ed WHERE ed.edge_id=a.edge_id AND ed.domain=?)")
            args.append(self.domain)
        if self.tiers is not None:
            clauses.append("a.tier IN (" + ",".join("?" for _ in self.tiers) + ")")
            args.extend(self.tiers)
        if not self.include_extracted:
            clauses.append("a.tier!='extracted'")
        return (" AND " + " AND ".join(clauses) if clauses else ""), args

    def outbound(self, current, rank, limit):
        clause, args = self.clause()
        ids = [r[0] for r in self.db.execute("SELECT a.edge_id FROM perf_adj a WHERE a.subject=?" + clause + " ORDER BY a." + rank + ",a.edge_id LIMIT ?", [current, *args, limit])]
        return self.load_edges(ids)

    def adjacent(self, current, level, limit, *, hydrate=True):
        clause, args = self.clause()
        inbound = " AND a.relation NOT IN ('contains','contains_nutrient','contains_analytical_flavonoid','chemical_gene_influence')" if level > 0 else ""
        subject_index = ' INDEXED BY perf_adj_subject_cover' if self.covering else ''
        object_index = ' INDEXED BY perf_adj_object_cover' if self.covering else ''
        if level > 0 and self.traversal_covering:
            object_index = ' INDEXED BY perf_adj_object_traversal_cover'
        # Each member of the global top K must occur in its branch's top K.
        # This retains the exact union ordering, including self-loop duplicates,
        # while avoiding a sort of every adjacent edge of a high-degree node.
        sql = ("WITH outgoing AS (SELECT a.edge_id,a.rank_default FROM perf_adj a" + subject_index +
               " WHERE a.subject=?" + clause + " ORDER BY a.rank_default,a.edge_id LIMIT ?), "
               "incoming AS (SELECT a.edge_id,a.rank_default FROM perf_adj a" + object_index +
               " WHERE a.object=?" + clause + inbound + " ORDER BY a.rank_default,a.edge_id LIMIT ?), "
               "adjacent AS (SELECT * FROM outgoing UNION SELECT * FROM incoming) "
               "SELECT edge_id FROM adjacent ORDER BY rank_default,edge_id LIMIT ?")
        ids = [r[0] for r in self.db.execute(sql, [current, *args, limit, current, *args, limit, limit])]
        if not hydrate:
            if not ids:
                return []
            rows = {r['edge_id']: dict(r) for r in self.db.execute(
                'SELECT edge_id id,edge_id,subject,object FROM perf_adj WHERE edge_id IN (' +
                ','.join('?' for _ in ids) + ')', ids)}
            return [rows[i] for i in ids]
        return self.load_edges(ids)
