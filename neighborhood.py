import numpy as np
import math
import networkx as nx
import tensorflow as tf
from neighborhood_model import NeighborhoodModel
from load import load_to_networkx, read_dataset
from datetime import datetime
from os import mkdir
from os.path import exists
from utils import create_demands, append_row_to_log, create_node_embeddings
from utils import add_features_sparse, create_node_bias, restore_params
from utils import sparse_matrix_to_tensor, features_to_demands, random_walk_neighborhoods
from utils import add_features, adj_mat_to_node_bias
from plot import plot_flow_graph_sparse, plot_flow_graph
from constants import BIG_NUMBER, LINE
from dataset import DatasetManager, Series, DataSeries


PRINT_THRESHOLD = 100


class NeighborhoodMCF:

    def __init__(self, params):
        self.params = params
        self.timestamp = datetime.now().strftime('%m-%d-%Y-%H-%M-%S')
        graph_names = '-'.join(params['train_graph_names'])
        self.output_folder = '{0}/{1}-{2}/'.format(params['output_folder'], graph_names, self.timestamp)
        self.num_node_features = 2

        file_paths = {
            Series.TRAIN: {},
            Series.VALID: {},
            Series.TEST: {}
        }
        dataset_path = 'datasets/{0}_{1}.txt'
        for dataset_name, graph_name in zip(self.params['train_dataset_names'], self.params['train_graph_names']):
            file_paths[Series.TRAIN][graph_name] = dataset_path.format(dataset_name, 'train')
            file_paths[Series.VALID][graph_name] = dataset_path.format(dataset_name, 'valid')
        
        for dataset_name, graph_name in zip(self.params['test_dataset_names'], self.params['test_graph_names']):
            file_paths[Series.TEST][graph_name] = dataset_path.format(dataset_name, 'test')

        self.dataset = DatasetManager(file_paths=file_paths, params=self.params['batch_params'])

    def train(self):

        # Load Graphs
        graphs, _, num_nodes = self._load_graphs()

        n_neighborhoods = self.params['num_neighborhoods']
        embedding_size = 2 * n_neighborhoods

        # Initialize model
        model = NeighborhoodModel(params=self.params)

        # Model placeholders
        node_ph, demands_ph, adj_ph, neighborhoods_ph, node_embedding_ph, \
            dropout_keep_ph = self.create_placeholders(model, num_nodes, embedding_size, n_neighborhoods)

        # Create model
        model.build(node_features=node_ph,
                    demands=demands_ph,
                    node_embeddings=node_embedding_ph,
                    neighborhoods=neighborhoods_ph,
                    adj=adj_ph,
                    num_output_features=num_nodes,
                    dropout_keep_prob=dropout_keep_ph)
        model.init()

        # Create output folder and initialize logging
        if not exists(self.output_folder):
            mkdir(self.output_folder)

        log_headers = ['Epoch', 'Avg Train Loss', 'Avg Valid Loss']
        log_path = self.output_folder + 'log.csv'
        append_row_to_log(log_headers, log_path)

        # Load training and validation datasets
        self.dataset.load(series=Series.TRAIN, graphs=graphs, num_nodes=num_nodes, num_neighborhoods=n_neighborhoods)
        self.dataset.load(series=Series.VALID, graphs=graphs, num_nodes=num_nodes, num_neighborhoods=n_neighborhoods)
        self.dataset.init(num_epochs=self.params['epochs'])

        # Variables for early stopping
        convergence_count = 0
        prev_loss = BIG_NUMBER

        batch_size = 1 if self.params['sparse'] else self.params['batch_size']

        for epoch in range(self.params['epochs']):

            print(LINE)
            print('Epoch {0}'.format(epoch))
            print(LINE)

            # Training Batches
            num_train_batches = int(math.ceil(self.dataset.num_train_points / batch_size))
            train_losses = []
            for i in range(num_train_batches):

                batch, indices = self.dataset.get_train_batch(batch_size=batch_size)

                if self.params['sparse']:
                    demands = features_to_demands(batch[DataSeries.NODE])
                    adj = batch[DataSeries.ADJ]
                else:
                    demands = [features_to_demands(n) for n in batch[DataSeries.NODE]]
                    adj = [a.todense() for a in batch[DataSeries.ADJ]]

                feed_dict = {
                    node_ph: batch[DataSeries.NODE],
                    demands_ph: demands,
                    adj_ph: adj,
                    node_embedding_ph: batch[DataSeries.EMBEDDING],
                    dropout_keep_ph: self.params['dropout_keep_prob']
                }

                # Provide neighborhood matrices
                for j in range(n_neighborhoods+1):

                    # Get the jth neighborhood for each element in the batch
                    neighborhood = [n[j] for n in batch[DataSeries.NEIGHBORHOOD]]

                    # Convert to dense matrices if necessary
                    if not self.params['sparse']:
                        neighborhood = [n.todense() for n in neighborhood]

                    feed_dict[neighborhoods_ph[j]] = np.array(neighborhood)

                outputs = model.run_train_step(feed_dict=feed_dict)
                avg_loss = outputs[0]
                loss = outputs[1]

                train_losses.append(avg_loss)
                self.dataset.report_losses(loss, indices)

                if not self.params['sparse'] or (i+1) % PRINT_THRESHOLD == 0:
                    if self.params['sparse']:
                        start = (i+1) - PRINT_THRESHOLD
                        avg_loss = np.average(train_losses[start:i+1])
                    print('Average train loss for batch {0}/{1}: {2}'.format(i+1, num_train_batches, avg_loss))

            print(LINE)

            # Validation Batches
            valid_batches = self.dataset.create_shuffled_batches(series=Series.VALID, batch_size=batch_size)
            num_valid_batches = len(valid_batches[DataSeries.NODE])
            valid_losses = []
            for i in range(num_valid_batches):

                node_features = valid_batches[DataSeries.NODE][i]
                adj = valid_batches[DataSeries.ADJ][i]
                embeddings = valid_batches[DataSeries.EMBEDDING][i]
                neighborhoods = valid_batches[DataSeries.NEIGHBORHOOD][i]

                if self.params['sparse']:
                    demands = features_to_demands(node_features)
                else:
                    demands = [features_to_demands(n) for n in node_features]
                    adj = [a.todense() for a in adj]

                feed_dict = {
                    node_ph: node_features,
                    demands_ph: demands,
                    adj_ph: adj,
                    node_embedding_ph: embeddings,
                    dropout_keep_ph: 1.0
                }

                # Provide neighborhood matrices
                for j in range(n_neighborhoods+1):

                    # Get the jth neighborhood for each element in the batch
                    neighborhood = [n[j] for n in neighborhoods]

                    # Convert to dense matrices if necessary
                    if not self.params['sparse']:
                        neighborhood = [n.todense() for n in neighborhood]

                    feed_dict[neighborhoods_ph[j]] = np.array(neighborhood)

                outputs = model.inference(feed_dict=feed_dict)
                avg_loss = outputs[0]
                valid_losses.append(avg_loss)

                if not self.params['sparse'] or (i+1) % PRINT_THRESHOLD == 0:
                    if self.params['sparse']:
                        start = (i+1) - PRINT_THRESHOLD
                        avg_loss = np.average(valid_losses[start:i+1])
                    print('Average valid loss for batch {0}/{1}: {2}'.format(i+1, num_valid_batches, avg_loss))

            print(LINE)

            avg_train_loss = np.average(train_losses)
            print('Average training loss: {0}'.format(avg_train_loss))

            avg_valid_loss = np.average(valid_losses)
            print('Average validation loss: {0}'.format(avg_valid_loss))

            log_row = [epoch, avg_train_loss, avg_valid_loss]
            append_row_to_log(log_row, log_path)

            # Early Stopping Counters
            if abs(prev_loss - avg_valid_loss) < self.params['early_stop_threshold']:
                convergence_count += 1
            else:
                convergence_count = 0

            if avg_valid_loss < prev_loss:
                print('Saving model...')
                model.save(self.output_folder)
                prev_loss = avg_valid_loss

            if convergence_count >= self.params['patience']:
                print('Early Stopping.')
                break

    def test(self, model_path):
        # Load Graphs
        _, graphs, num_nodes = self._load_graphs()

        n_neighborhoods = self.params['num_neighborhoods']    
        embedding_size = 2 * n_neighborhoods

        # Initialize model
        model = NeighborhoodModel(params=self.params)

        # Model placeholders
        node_ph, demands_ph, adj_ph, neighborhoods_ph, node_embedding_ph, \
            dropout_keep_ph = self.create_placeholders(model, num_nodes, embedding_size, n_neighborhoods)

        # Create model
        model.build(node_features=node_ph,
                    demands=demands_ph,
                    node_embeddings=node_embedding_ph,
                    neighborhoods=neighborhoods_ph,
                    adj=adj_ph,
                    num_output_features=num_nodes,
                    dropout_keep_prob=dropout_keep_ph)
        model.init()
        model.restore(model_path)

        # Load test data
        self.dataset.load(series=Series.TEST, num_nodes=num_nodes, graphs=graphs, num_neighborhoods=n_neighborhoods)
        test_batches = self.dataset.create_batches(series=Series.TEST, batch_size=1, shuffle=False)

        num_test_batches = len(test_batches[DataSeries.NODE])

        for i in range(num_test_batches):

            node_features = test_batches[DataSeries.NODE][i]
            adj = test_batches[DataSeries.ADJ][i]
            embeddings = test_batches[DataSeries.EMBEDDING][i]
            neighborhoods = test_batches[DataSeries.NEIGHBORHOOD][i]
            demands = features_to_demands(node_features)

            # Ensure correct tensor rank for dense mode
            if not self.params['sparse']:
                node_features = [node_features]
                adj = [adj.todense()]
                embeddings = [embeddings]
                demands = [demands]

            feed_dict = {
                node_ph: node_features,
                demands_ph: demands,
                adj_ph: adj,
                node_embedding_ph: embeddings,
                dropout_keep_ph: 1.0
            }

            # Provide neighborhood matrices
            for j, neighborhood in enumerate(neighborhoods):

                # Convert to dense matrices if necessary
                if not self.params['sparse']:
                    neighborhood = [neighborhood.todense()]

                feed_dict[neighborhoods_ph[j]] = np.array(neighborhood)

            outputs = model.inference(feed_dict=feed_dict)

            graph_name = test_batches[DataSeries.GRAPH_NAME][i]
            graph = graphs[graph_name]

            flow_cost = outputs[1]
            flows = outputs[2]
            flow_proportions = outputs[3]

            if self.params['sparse']:
                flow_graph = add_features_sparse(graph, demands=node_features, flows=flows,
                                                 proportions=flow_proportions)
            else:
                flow_graph = add_features(graph, demands=node_features[0], flows=flows[0],
                                          proportions=flow_proportions[0])

            # Write output graph to Graph XML
            nx.write_gexf(flow_graph, '{0}graph-{1}-{2}.gexf'.format(model_path, graph_name, i))

            if self.params['plot_flows']:
                if self.params['sparse']:
                    plot_flow_graph_sparse(flow_graph, flows, '{0}flows-{1}-{2}.png'.format(model_path, graph_name, i))
                    plot_flow_graph_sparse(flow_graph, flow_proportions, '{0}flow-prop-{1}-{2}.png'.format(model_path, graph_name, i))
                else:
                    plot_flow_graph(flow_graph, flows[0], '{0}flows-{1}-{2}.png'.format(model_path, graph_name, i))
                    plot_flow_graph(flow_graph, flow_proportions[0], '{0}flow-prop-{1}-{2}.png'.format(model_path, graph_name, i))


    def create_placeholders(self, model, num_nodes, embedding_size, num_neighborhoods):
        node_shape = [None, num_nodes, self.num_node_features]
        demands_shape = [None, num_nodes, 1]
        adj_shape = [None, num_nodes, num_nodes]
        neighborhood_shape = [None, num_nodes, num_nodes]
        embedding_shape = [None, num_nodes, embedding_size]

        if self.params['sparse']:
            node_shape = node_shape[1:]
            demands_shape = demands_shape[1:]
            adj_shape = adj_shape[1:]
            neighborhood_shape = neighborhood_shape[1:]
            embedding_shape = embedding_shape[1:]

        node_ph = model.create_placeholder(dtype=tf.float32,
                                           shape=node_shape,
                                           name='node-ph',
                                           is_sparse=False)
        demands_ph = model.create_placeholder(dtype=tf.float32,
                                              shape=demands_shape,
                                              name='demands-ph',
                                              is_sparse=False)
        adj_ph = model.create_placeholder(dtype=tf.float32,
                                          shape=adj_shape,
                                          name='adj-ph',
                                          is_sparse=self.params['sparse'])
        node_embedding_ph = model.create_placeholder(dtype=tf.float32,
                                                     shape=embedding_shape,
                                                     name='node-embedding-ph',
                                                     is_sparse=False)
        dropout_keep_ph = model.create_placeholder(dtype=tf.float32,
                                                   shape=(),
                                                   name='dropout-keep-ph',
                                                   is_sparse=False)

        neighborhoods = []
        for i in range(num_neighborhoods+1):
            ph = model.create_placeholder(dtype=tf.float32,
                                          shape=neighborhood_shape,
                                          name='neighborhood-{0}-ph'.format(i),
                                          is_sparse=self.params['sparse'])
            neighborhoods.append(ph)

        return node_ph, demands_ph, adj_ph, neighborhoods, node_embedding_ph, dropout_keep_ph
    
    def _num_neighborhoods(self, graph):
        if 'num_neighborhoods' in self.params:
            return self.params['num_neighborhoods']
        return max(2, int(math.log(graph.number_of_nodes())))

    def _load_graphs(self):
        graph_path = 'graphs/{0}.tntp'

        train_graphs = {}
        for graph_name in self.params['train_graph_names']:
            graph = load_to_networkx(path=graph_path.format(graph_name))
            train_graphs[graph_name] = graph

        test_graphs = {}
        for graph_name in self.params['test_graph_names']:
            graph = load_to_networkx(path=graph_path.format(graph_name))
            test_graphs[graph_name] = graph

        num_train_nodes = np.max([g.number_of_nodes() for g in train_graphs.values()])
        num_test_nodes = np.max([g.number_of_nodes() for g in test_graphs.values()])

        return train_graphs, test_graphs, max(num_train_nodes, num_test_nodes)
