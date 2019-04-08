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
from dataset import DatasetManager, Series


PRINT_THRESHOLD = 100


class NeighborhoodMCF:

    def __init__(self, params):
        self.params = params
        self.timestamp = datetime.now().strftime('%m-%d-%Y-%H-%M-%S')
        self.output_folder = '{0}/{1}-{2}/'.format(params['output_folder'], params['graph_name'], self.timestamp)
        self.num_node_features = 2

        train_file = 'datasets/{0}_train.txt'.format(self.params['dataset_name'])
        valid_file = 'datasets/{0}_valid.txt'.format(self.params['dataset_name'])
        test_file = 'datasets/{0}_test.txt'.format(self.params['dataset_name'])
        self.dataset = DatasetManager(train_file, valid_file, test_file, params=self.params['batch_params'])

    def train(self):
        # Load graph
        graph_path = 'graphs/{0}.tntp'.format(self.params['graph_name'])
        graph = load_to_networkx(path=graph_path)

        # Create tensors for global graph properties
        adj_mat = nx.adjacency_matrix(graph)

        if self.params['sparse']:
            adj_tensor = sparse_matrix_to_tensor(adj_mat)
        else:
            adj_tensor = adj_mat.todense()

        # Graph properties
        num_nodes = graph.number_of_nodes()

        # Create neighborhoods
        n_neighborhoods = self._num_neighborhoods(graph)
        neighborhoods = random_walk_neighborhoods(adj_mat, k=n_neighborhoods)

        if self.params['sparse']:
            neighborhood_tensors = [sparse_matrix_to_tensor(m) for m in neighborhoods]
        else:
            neighborhood_tensors = [adj_mat_to_node_bias(m) for m in neighborhoods]

        # Create node embeddings
        node_embeddings = create_node_embeddings(graph)
        embedding_size = node_embeddings.shape[1]

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
        self.dataset.load(series=Series.TRAIN, num_nodes=num_nodes)
        self.dataset.load(series=Series.VALID, num_nodes=num_nodes)
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

                node_features, indices = self.dataset.get_train_batch(batch_size=batch_size)

                if self.params['sparse']:
                    node_features = node_features[0]
                    demands = features_to_demands(node_features)
                else:
                    demands = [features_to_demands(n) for n in node_features]

                feed_dict = {
                    node_ph: node_features,
                    demands_ph: demands,
                    adj_ph: adj_tensor,
                    node_embedding_ph: node_embeddings,
                    dropout_keep_ph: self.params['dropout_keep_prob']
                }

                # Provide neighborhood matrices
                for j in range(len(neighborhood_tensors)):
                    feed_dict[neighborhoods_ph[j]] = neighborhood_tensors[j]

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
            num_valid_batches = len(valid_batches)
            valid_losses = []
            for i, node_features in enumerate(valid_batches):

                if self.params['sparse']:
                    node_features = node_features[0]
                    demands = features_to_demands(node_features)
                else:
                    demands = [features_to_demands(n) for n in node_features]

                feed_dict = {
                    node_ph: node_features,
                    demands_ph: demands,
                    adj_ph: adj_tensor,
                    node_embedding_ph: node_embeddings,
                    dropout_keep_ph: 1.0
                }

                for j in range(len(neighborhood_tensors)):
                    feed_dict[neighborhoods_ph[j]] = neighborhood_tensors[j]

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
        # Load graph
        graph_path = 'graphs/{0}.tntp'.format(self.params['graph_name'])
        graph = load_to_networkx(path=graph_path)

        # Create tensors for global graph properties
        adj_mat = nx.adjacency_matrix(graph)

        if self.params['sparse']:
            adj_tensor = sparse_matrix_to_tensor(adj_mat)
        else:
            adj_tensor = adj_mat.todense()

        # Create neighborhoods
        n_neighborhoods = self._num_neighborhoods(graph)
        neighborhoods = random_walk_neighborhoods(adj_mat, k=n_neighborhoods)
        
        if self.params['sparse']:
            neighborhood_tensors = [sparse_matrix_to_tensor(m) for m in neighborhoods]
        else:
            neighborhood_tensors = [adj_mat_to_node_bias(m) for m in neighborhoods]

        # Graph properties
        num_nodes = graph.number_of_nodes()

        # Create node embeddings
        node_embeddings = create_node_embeddings(graph)
        embedding_size = node_embeddings.shape[1]

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
        self.dataset.load(series=Series.TEST, num_nodes=num_nodes)
        test_batches = self.dataset.create_shuffled_batches(series=Series.TEST, batch_size=1)

        for i, node_features in enumerate(test_batches):

            if self.params['sparse']:
                node_features = node_features[0]
                demands = features_to_demands(node_features)
            else:
                demands = [features_to_demands(n) for n in node_features]

            feed_dict = {
                node_ph: node_features,
                demands_ph: demands,
                adj_ph: adj_tensor,
                node_embedding_ph: node_embeddings,
                dropout_keep_ph: 1.0
            }

            for j in range(len(neighborhood_tensors)):
                feed_dict[neighborhoods_ph[j]] = neighborhood_tensors[j]

            outputs = model.inference(feed_dict=feed_dict)

            flow_cost = outputs[1]
            flows = outputs[2]
            flow_proportions = outputs[3]

            if self.params['sparse']:
                flow_graph = add_features_sparse(graph, demands=node_features[0], flows=flows,
                                                 proportions=flow_proportions)
            else:
                flow_graph = add_features(graph, demands=node_features[0], flows=flows[0],
                                          proportions=flow_proportions[0])

            # Write output graph to Graph XML
            nx.write_gexf(flow_graph, '{0}graph-{1}.gexf'.format(model_path, i))

            if self.params['plot_flows']:
                if self.params['sparse']:
                    plot_flow_graph_sparse(flow_graph, flows, '{0}flows-{1}.png'.format(model_path, i))
                    plot_flow_graph_sparse(flow_graph, flow_proportions, '{0}flow-prop-{1}.png'.format(model_path, i))
                else:
                    plot_flow_graph(flow_graph, flows[0], '{0}flows-{1}.png'.format(model_path, i))
                    plot_flow_graph(flow_graph, flow_proportions[0], '{0}flow-prop-{1}.png'.format(model_path, i))


    def create_placeholders(self, model, num_nodes, embedding_size, num_neighborhoods):
        node_shape = [None, num_nodes, self.num_node_features]
        demands_shape = [None, num_nodes, 1]

        if self.params['sparse']:
            node_shape = node_shape[1:]
            demands_shape = demands_shape[1:]

        node_ph = model.create_placeholder(dtype=tf.float32,
                                           shape=node_shape,
                                           name='node-ph',
                                           is_sparse=False)
        demands_ph = model.create_placeholder(dtype=tf.float32,
                                              shape=demands_shape,
                                              name='demands-ph',
                                              is_sparse=False)
        adj_ph = model.create_placeholder(dtype=tf.float32,
                                          shape=[None, num_nodes],
                                          name='adj-ph',
                                          is_sparse=self.params['sparse'])

        neighborhoods = []
        for i in range(num_neighborhoods+1):
            ph = model.create_placeholder(dtype=tf.float32,
                                          shape=[None, num_nodes],
                                          name='neighborhood-{0}-ph'.format(i),
                                          is_sparse=self.params['sparse'])
            neighborhoods.append(ph)

        node_embedding_ph = model.create_placeholder(dtype=tf.float32,
                                                     shape=[num_nodes, embedding_size],
                                                     name='node-embedding-ph',
                                                     is_sparse=False)
        dropout_keep_ph = model.create_placeholder(dtype=tf.float32,
                                                   shape=(),
                                                   name='dropout-keep-ph',
                                                   is_sparse=False)
        return node_ph, demands_ph, adj_ph, neighborhoods, node_embedding_ph, dropout_keep_ph
    
    def _num_neighborhoods(self, graph):
        if 'num_neighborhoods' in self.params:
            return self.params['num_neighborhoods']
        return max(2, int(math.log(graph.number_of_nodes())))
