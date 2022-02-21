# -*- coding: utf-8 -*-
#  Copyright (c) 2022, Markus Binsteiner
#
#  Mozilla Public License, version 2.0 (see LICENSE or https://www.mozilla.org/en-US/MPL/2.0/)
import typing
from xml.dom import minidom

from kiara.utils.output import DictTabularWrap, TabularWrap
from kiara_modules.core.database import SqliteTableSchema
from kiara_modules.core.defaults import DEFAULT_DB_CHUNK_SIZE

from kiara_modules.network_analysis.defaults import (
    ID_COLUMN_NAME,
    LABEL_COLUMN_NAME,
    SOURCE_COLUMN_NAME,
    TARGET_COLUMN_NAME,
    TableType,
)

if typing.TYPE_CHECKING:
    import networkx as nx
    import pyarrow as pa
    from sqlalchemy import Metadata, Table  # noqa

    from kiara_modules.network_analysis.metadata_models import NetworkData


class NetworkDataTabularWrap(TabularWrap):
    def __init__(self, db: "NetworkData", table_type: TableType):
        self._db: NetworkData = db
        self._table_type: TableType = table_type
        super().__init__()

    @property
    def _table_name(self):
        return self._table_type.value

    def retrieve_number_of_rows(self) -> int:

        from sqlalchemy import text

        with self._db.get_sqlalchemy_engine().connect() as con:
            result = con.execute(text(f"SELECT count(*) from {self._table_name}"))
            num_rows = result.fetchone()[0]

        return num_rows

    def retrieve_column_names(self) -> typing.Iterable[str]:

        from sqlalchemy import inspect

        engine = self._db.get_sqlalchemy_engine()
        inspector = inspect(engine)
        columns = inspector.get_columns(self._table_type.value)
        result = [column["name"] for column in columns]
        return result

    def slice(
        self, offset: int = 0, length: typing.Optional[int] = None
    ) -> "TabularWrap":

        from sqlalchemy import text

        query = f"SELECT * FROM {self._table_name}"
        if length:
            query = f"{query} LIMIT {length}"
        else:
            query = f"{query} LIMIT {self.num_rows}"
        if offset > 0:
            query = f"{query} OFFSET {offset}"
        with self._db.get_sqlalchemy_engine().connect() as con:
            result = con.execute(text(query))
            result_dict: typing.Dict[str, typing.List[typing.Any]] = {}
            for cn in self.column_names:
                result_dict[cn] = []
            for r in result:
                for i, cn in enumerate(self.column_names):
                    result_dict[cn].append(r[i])

        return DictTabularWrap(result_dict)

    def to_pydict(self) -> typing.Mapping:

        from sqlalchemy import text

        query = f"SELECT * FROM {self._table_name}"

        with self._db.get_sqlalchemy_engine().connect() as con:
            result = con.execute(text(query))
            result_dict: typing.Dict[str, typing.List[typing.Any]] = {}
            for cn in self.column_names:
                result_dict[cn] = []
            for r in result:
                for i, cn in enumerate(self.column_names):
                    result_dict[cn].append(r[i])

        return result_dict


def convert_graphml_type_to_sqlite(data_type: str) -> str:

    type_map = {
        "boolean": "INTEGER",
        "int": "INTEGER",
        "long": "INTEGER",
        "float": "REAL",
        "double": "REAL",
        "string": "TEXT",
    }

    return type_map[data_type]


def parse_graphml_file(path):
    """Adapted from the pygrahml Python library.

    Authors:
      - Hadrien Mary hadrien.mary@gmail.com
      - Nick Hamilton n.hamilton@imb.uq.edu.au

    Copyright (c) 2011, Hadrien Mary
    License: BSD 3-Clause

    """

    g = None
    with open(path, "r") as f:
        dom = minidom.parse(f)
        root = dom.getElementsByTagName("graphml")[0]
        graph = root.getElementsByTagName("graph")[0]
        name = graph.getAttribute("id")

        from pygraphml import Graph

        g = Graph(name)

        # Get attributes
        edge_map = {}
        node_map = {}

        edge_props = {}
        node_props = {}
        for attr in root.getElementsByTagName("key"):
            n_id = attr.getAttribute("id")
            name = attr.getAttribute("attr.name")
            for_type = attr.getAttribute("for")
            attr_type = attr.getAttribute("attr.type")
            if for_type == "edge":
                edge_map[n_id] = name
                edge_props[name] = {
                    "data_type": convert_graphml_type_to_sqlite(attr_type)
                }
            else:
                node_map[n_id] = name
                node_props[name] = {
                    "data_type": convert_graphml_type_to_sqlite(attr_type)
                }

        node_props_sorted = {}
        for key in sorted(node_map.keys()):
            node_props_sorted[node_map[key]] = node_props[node_map[key]]
        edge_props_sorted = {}
        for key in sorted(edge_map.keys()):
            edge_props_sorted[edge_map[key]] = edge_props[edge_map[key]]

        # Get nodes
        for node in graph.getElementsByTagName("node"):
            n = g.add_node(id=node.getAttribute("id"))

            for attr in node.getElementsByTagName("data"):
                key = attr.getAttribute("key")
                mapped = node_map[key]
                if attr.firstChild:
                    n[mapped] = attr.firstChild.data
                else:
                    n[mapped] = ""

        # Get edges
        for edge in graph.getElementsByTagName("edge"):
            source = edge.getAttribute("source")
            dest = edge.getAttribute("target")

            # source/target attributes refer to IDs: http://graphml.graphdrawing.org/xmlns/1.1/graphml-structure.xsd
            e = g.add_edge_by_id(source, dest)

            for attr in edge.getElementsByTagName("data"):
                key = attr.getAttribute("key")
                mapped = edge_map[key]
                if attr.firstChild:
                    e[mapped] = attr.firstChild.data
                else:
                    e[mapped] = ""

    return (g, edge_props_sorted, node_props_sorted)


def insert_table_data_into_network_graph(
    network_data: "NetworkData",
    edges_table: "pa.Table",
    edges_schema: SqliteTableSchema,
    nodes_table: typing.Optional["pa.Table"] = None,
    nodes_schema: typing.Optional[SqliteTableSchema] = None,
    chunk_size: int = DEFAULT_DB_CHUNK_SIZE,
):

    added_node_ids = set()

    if nodes_table is not None:
        for batch in nodes_table.to_batches(chunk_size):
            batch_dict = batch.to_pydict()

            if nodes_schema:
                column_map = nodes_schema.column_map
            else:
                column_map = {}

            for k, v in column_map.items():
                if k in batch_dict.keys():
                    if k == ID_COLUMN_NAME and v == LABEL_COLUMN_NAME:
                        _data = batch_dict.get(k)
                    else:
                        _data = batch_dict.pop(k)
                        if v in batch_dict.keys():
                            raise Exception(
                                "Duplicate nodes column name after mapping: {v}"
                            )
                    batch_dict[v] = _data

            ids = batch_dict[ID_COLUMN_NAME]
            data = [dict(zip(batch_dict, t)) for t in zip(*batch_dict.values())]
            network_data.insert_nodes(*data)

            added_node_ids.update(ids)

    for batch in edges_table.to_batches(chunk_size):

        batch_dict = batch.to_pydict()
        if edges_schema:
            column_map = edges_schema.column_map
        else:
            column_map = {}
        for k, v in column_map.items():
            if k in batch_dict.keys():
                _data = batch_dict.pop(k)
                if v in batch_dict.keys():
                    raise Exception("Duplicate edges column name after mapping: {v}")
                batch_dict[v] = _data

        data = [dict(zip(batch_dict, t)) for t in zip(*batch_dict.values())]

        all_node_ids = network_data.insert_edges(
            *data,
            existing_node_ids=added_node_ids,
        )
        added_node_ids.update(all_node_ids)


def extract_edges_as_table(graph: "nx.Graph"):

    # adapted from networx code
    # License: 3-clause BSD license
    # Copyright (C) 2004-2022, NetworkX Developers

    import networkx as nx
    import pyarrow as pa

    edgelist = graph.edges(data=True)
    source_nodes = [s for s, _, _ in edgelist]
    target_nodes = [t for _, t, _ in edgelist]

    all_attrs: typing.Set[str] = set().union(*(d.keys() for _, _, d in edgelist))  # type: ignore

    if SOURCE_COLUMN_NAME in all_attrs:
        raise nx.NetworkXError(
            f"Source name {SOURCE_COLUMN_NAME} is an edge attribute name"
        )
    if SOURCE_COLUMN_NAME in all_attrs:
        raise nx.NetworkXError(
            f"Target name {SOURCE_COLUMN_NAME} is an edge attribute name"
        )

    nan = float("nan")
    edge_attr = {k: [d.get(k, nan) for _, _, d in edgelist] for k in all_attrs}

    edge_lists = {
        SOURCE_COLUMN_NAME: source_nodes,
        TARGET_COLUMN_NAME: target_nodes,
    }

    edge_lists.update(edge_attr)
    edges_table = pa.Table.from_pydict(mapping=edge_lists)

    return edges_table


def extract_nodes_as_table(graph: "nx.Graph"):

    # adapted from networx code
    # License: 3-clause BSD license
    # Copyright (C) 2004-2022, NetworkX Developers

    import networkx as nx
    import pyarrow as pa

    nodelist = graph.nodes(data=True)

    node_ids = [n for n, _ in nodelist]

    all_attrs: typing.Set[str] = set().union(*(d.keys() for _, d in nodelist))  # type: ignore

    if ID_COLUMN_NAME in all_attrs:
        raise nx.NetworkXError(
            f"Id column name {ID_COLUMN_NAME} is an node attribute name"
        )
    if SOURCE_COLUMN_NAME in all_attrs:
        raise nx.NetworkXError(
            f"Target name {SOURCE_COLUMN_NAME} is an edge attribute name"
        )

    nan = float("nan")
    node_attr = {k: [d.get(k, nan) for _, d in nodelist] for k in all_attrs}

    node_attr[ID_COLUMN_NAME] = node_ids
    nodes_table = pa.Table.from_pydict(mapping=node_attr)

    return nodes_table
