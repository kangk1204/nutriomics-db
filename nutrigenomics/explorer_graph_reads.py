"""Lazy batched graph hydration; preserve the original bounded traversal exactly."""
from __future__ import annotations

import hashlib

from .explorer_performance import has_index


class GraphReads:
    def __init__(self, db, decode_node, decode_edge, domain, tiers, include_extracted):
        self.db, self.decode_node, self.decode_edge = db, decode_node, decode_edge
        self.domain, self.tiers, self.include_extracted = domain, tiers, include_extracted
        self.indexed = has_index(db, "perf_adj")
        self.covering = self.indexed and bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='perf_adj_object_cover'").fetchone())
        self.traversal_covering = self.indexed and bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='perf_adj_object_traversal_cover'").fetchone())
        self.kind_projection = bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='node_kind'").fetchone())
        self.nodes, self.edges = {}, {}
        self.kinds = {}
        self.pending = {}
        self.domain_function = None
        if self.indexed and domain != 'all':
            # Read the small covering domain projection once. A correlated
            # EXISTS for every candidate caused scattered HDD reads during
            # health-path lookahead, despite bounded result/display sizes.
            members = frozenset(row[0] for row in db.execute(
                'SELECT edge_id FROM edge_domain WHERE domain=?', (domain,)).fetchall())
            self.domain_function = 'ng_graph_domain_' + hashlib.sha256(domain.encode()).hexdigest()[:12]
            db.create_function(self.domain_function, 1, lambda identifier: int(identifier is not None and identifier in members), deterministic=True)

    def prefetch_adjacent(self, identifiers, level, limit):
        """Batch queued lookups without changing traversal or per-node top K."""
        if not identifiers or (identifiers[0], level, limit) in self.pending:
            return
        identifiers = list(dict.fromkeys(identifiers))[:32]
        if len(identifiers) < 2:
            return
        clause, args = self.clause()
        selected = {identifier: {} for identifier in identifiers}
        for direction in ('subject', 'object'):
            inbound = " AND a.relation NOT IN ('contains','contains_nutrient','contains_analytical_flavonoid','chemical_gene_influence')" if direction == 'object' and level > 0 else ''
            index = ''
            if self.covering:
                name = 'perf_adj_object_traversal_cover' if direction == 'object' and level > 0 and self.traversal_covering else 'perf_adj_' + direction + '_cover'
                index = ' INDEXED BY ' + name
            # A window over IN (...) ranks every neighbor before its outer
            # position<=K filter. Selecting the opposite endpoint also forces
            # random table reads beyond the covering adjacency index. Keep each
            # node/direction top K bounded inside its indexed subquery, then
            # retain only the final union's IDs. Its endpoints are read when
            # that queued node is actually visited, not for unused lookahead.
            branch = ('SELECT ? AS origin,edge_id,rank_default FROM (SELECT a.edge_id,a.rank_default '
                      'FROM perf_adj a' + index + ' WHERE a.' + direction + '=?' + clause + inbound +
                      ' ORDER BY a.rank_default,a.edge_id LIMIT ?)')
            sql = ' UNION ALL '.join(branch for _ in identifiers)
            parameters = [value for identifier in identifiers for value in (identifier, identifier, *args, limit)]
            for row in self.db.execute(sql, parameters):
                selected[row['origin']][row['edge_id']] = dict(row)
        ordered = {identifier: sorted(items.values(), key=lambda r: (r['rank_default'], r['edge_id']))[:limit]
                   for identifier, items in selected.items()}
        for identifier, rows in ordered.items():
            self.pending[(identifier, level, limit)] = [r['edge_id'] for r in rows]

    def node_kind(self, identifier):
        if identifier in self.nodes:
            return self.nodes[identifier]['kind']
        if identifier not in self.kinds:
            # Health-path lookahead visits at most 240 nodes. A primary-key
            # lookup keeps cold work bounded too, instead of scanning every
            # regulatory-region node on each request or cache initialization.
            # Canonical releases include an exact id/kind mirror, avoiding
            # pages containing large node metadata for this two-field lookup.
            row = self.db.execute("SELECT kind FROM node_kind WHERE id=?", (identifier,)).fetchone() if self.kind_projection else None
            if row is None:
                row = self.db.execute("SELECT kind FROM node WHERE id=?", (identifier,)).fetchone()
            self.kinds[identifier] = row[0] if row is not None else None
        return self.kinds[identifier]

    def endpoints(self, identifiers):
        """Hydrate only a popped node's exact top K, preserving its order."""
        rows = {}
        for start in range(0, len(identifiers), 500):
            batch = identifiers[start:start + 500]
            for row in self.db.execute(
                'SELECT edge_id id,edge_id,subject,object FROM perf_adj WHERE edge_id IN (' +
                ','.join('?' for _ in batch) + ')', batch
            ):
                rows[row['edge_id']] = dict(row)
        return [rows[identifier] for identifier in identifiers]

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
            if self.domain_function:
                clauses.append(self.domain_function + '(a.edge_id)')
            else:
                clauses.append("EXISTS(SELECT 1 FROM edge_domain ed WHERE ed.edge_id=a.edge_id AND ed.domain=?)")
                args.append(self.domain)
        if self.tiers is not None:
            clauses.append("a.tier IN (" + ",".join("?" for _ in self.tiers) + ")")
            args.extend(self.tiers)
        if not self.include_extracted:
            clauses.append("a.tier!='extracted'")
        return (" AND " + " AND ".join(clauses) if clauses else ""), args

    def outbound(self, current, rank, limit, *, hydrate=True):
        clause, args = self.clause()
        if not hydrate:
            return [dict(r) for r in self.db.execute(
                "SELECT a.edge_id AS id,a.subject,a.object,a.usable FROM perf_adj a WHERE a.subject=?" +
                clause + " ORDER BY a." + rank + ",a.edge_id LIMIT ?", [current, *args, limit])]
        ids = [r[0] for r in self.db.execute("SELECT a.edge_id FROM perf_adj a WHERE a.subject=?" + clause + " ORDER BY a." + rank + ",a.edge_id LIMIT ?", [current, *args, limit])]
        return self.load_edges(ids)

    def adjacent(self, current, level, limit, *, hydrate=True):
        ready = self.pending.pop((current, level, limit), None)
        if ready is not None:
            return self.load_edges(ready) if hydrate else self.endpoints(ready)
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
            return self.endpoints(ids)
        return self.load_edges(ids)
