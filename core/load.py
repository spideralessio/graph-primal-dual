import networkx as nx
import numpy as np
import pickle
import gzip
import scipy.sparse as sp
from os.path import exists
from utils.constants import SMALL_NUMBER
from annoy import AnnoyIndex


def load_to_networkx(path):
    graph = nx.DiGraph()

    edge_features = {}

    with open(path, 'r') as net_file:
        metadata = True

        nodes = set()

        for line in net_file:
            if len(line) == 0:
                continue
            if line.startswith('~'):
                metadata = False
                continue
            if metadata:
                continue

            edge_elems = line.split('\t')
            init, term, features = _parse_edge_features(edge_elems)

            if init == -1 or term == -1:
                continue

            nodes.add(init)
            nodes.add(term)

            edge = (init, term)
            edge_features[edge] = features

    # Rename nodes 0,...,n-1. Sort node set to ensure a deterministic naming.
    node_map = {}
    for i, node in enumerate(sorted(nodes)):
        node_map[node] = i
        graph.add_node(i)

    # Add edges features
    for (src, dst), features in edge_features.items():
        graph.add_edge(node_map[src], node_map[dst], **features)

    # We make the graph strongly connected to ensure that any combination of source / sink
    # constitutes a valid problem
    comp = nx.strongly_connected_components(graph)
    try:
        c1 = list(comp.__next__())
        while True:
            c2 = list(comp.__next__())

            # For now, we don't import any edge features
            graph.add_edge(c1[0], c2[0])
            graph.add_edge(c2[0], c1[0])

            c1 = c2

    except StopIteration:
        pass

    return graph


def write_dataset(dataset, output_path):
    """
    dataset is a list of dictionaries of the form { 'dem': [], 'cap': [] }
    """
    with gzip.GzipFile(output_path, 'ab') as output_file:
        for data_point in dataset:

            # Convert demands into node features
            node_features = np.zeros(shape=(data_point['dem'].shape[0], 2))
            for i, demand in enumerate(data_point['dem']):
                if demand[0] > 0:
                    node_features[i][0] = demand[0]
                elif demand[0] < 0:
                    node_features[i][1] = -demand[0]

            compressed_demands = sp.csr_matrix(node_features)
            pickle.dump({'dem': compressed_demands, 'cap': data_point['cap']}, output_file)


def read_dataset(data_path):
    dataset = []

    with gzip.GzipFile(data_path, 'rb') as data_file:
        try:
            while True:
                data_dict = pickle.load(data_file)
                dataset.append({'dem': data_dict['dem'], 'cap': data_dict['cap']})
        except EOFError:
            pass
    return dataset


def load_embeddings(index_path, embedding_size, num_nodes):
    # Load Annoy index which stores the embedded vectors
    index = AnnoyIndex(embedding_size)
    index.load(index_path)

    embeddings = [index.get_item_vector(i) for i in range(num_nodes)]

    # Unload the index to save memory (loading mmaps the index file)
    index.unload()

    # V x D matrix of embeddings
    return np.array(embeddings)


def _parse_edge_features(edge_elems):
    init = int(_get_index_if_exists(edge_elems, 1, 0))
    term = int(_get_index_if_exists(edge_elems, 2, 0))

    features = {
        'capacity': float(_get_index_if_exists(edge_elems, 3, 0.0)),
        'length': float(_get_index_if_exists(edge_elems, 4, 0.0)),
        'free_flow_time': float(_get_index_if_exists(edge_elems, 5, 0.0)),
        'b': float(_get_index_if_exists(edge_elems, 6, 0.0)),
        'power': float(_get_index_if_exists(edge_elems, 7, 0.0)),
        'speed_limit': float(_get_index_if_exists(edge_elems, 8, 0.0)),
        'toll': float(_get_index_if_exists(edge_elems, 9, 0.0)),
        'link_type': int(_get_index_if_exists(edge_elems, 10, 0.0))
    }

    return init-1, term-1, features


def _get_index_if_exists(array, index, default):
    if len(array) <= index:
        return default
    if len(array[index]) == 0:
        return default
    return array[index]