import tensorflow as tf
import numpy as np
from base_model import Model
from layers import MLP, SparseGAT, SparseMinCostFlow, Gate, MinCostFlow
from cost_functions import get_cost_function


class SparseMCFModel(Model):

    def __init__(self, params, name='sparse-mcf-model'):
        super(SparseMCFModel, self).__init__(params, name)
        self.cost_fn = get_cost_function(name=params['cost_fn'],
                                         constant=params['cost_constant'])

    def build(self, **kwargs):

        # V x 1 tensor which contains node features
        demands = kwargs['demands']

        # V x D' tensor which contains pre-computed node embeddings
        node_embeddings = kwargs['node_embeddings']

        # V x V sparse tensor containing the adjacency matrix
        adj = kwargs['adj']

        num_output_features = kwargs['num_output_features']

        with self._sess.graph.as_default():
            with tf.variable_scope(self.name, reuse=tf.AUTO_REUSE):

                adj_self_loops = tf.sparse.add(adj, tf.sparse.eye(num_rows=tf.shape(adj)[0]))

                # Node encoding
                encoder = MLP(hidden_sizes=[],
                              output_size=self.params['node_encoding'],
                              activation=tf.nn.relu,
                              name='node-encoder')
                node_encoding = encoder(inputs=tf.concat([node_embeddings, demands], axis=1))

                node_gat = SparseGAT(input_size=self.params['node_encoding'],
                                     output_size=self.params['node_encoding'],
                                     num_heads=self.params['num_heads'],
                                     name='node-gat')
                gate = Gate(name='node-gate')

                # Stitch together graph and gating layers
                for _ in range(self.params['graph_layers']):
                    next_encoding = node_gat(inputs=node_encoding, adj_matrix=adj_self_loops)
                    node_encoding = gate(inputs=next_encoding, prev_state=node_encoding)

                # Compute flow proportions
                decoder = MLP(hidden_sizes=[],
                              output_size=num_output_features,
                              activation=None,
                              name='node-decoder')
                pred_weights = decoder(inputs=node_encoding)

                flow_weight_pred = tf.sparse.softmax(adj * pred_weights, name='normalized-weights')

                # Compute minimum cost flow from flow weights
                #mcf_solver = SparseMinCostFlow(flow_iters=self.params['flow_iters'])
                #flow = mcf_solver(inputs=flow_weight_pred, demands=demands)

                # flow = tf.SparseTensor(indices=np.empty(shape=(0, 2), dtype=np.int64),
                #                values=[],
                #                dense_shape=flow_weight_pred.dense_shape)
                flow = flow_weight_pred
                prev_flow = flow
                index = tf.constant(0, dtype=tf.int64)

                def body(flow):
                    inflow = tf.sparse_reduce_sum(flow, axis=0)
                    inflow = tf.expand_dims(inflow, axis=1)
                    adjusted_inflow = tf.nn.relu(inflow - demands)
                    new_flow = flow_weight_pred * adjusted_inflow
                    return new_flow

                flow = tf.while_loop(cond=lambda x: True,
                                     body=body,
                                     loop_vars=[flow],
                                     parallel_iterations=1,
                                     maximum_iterations=self.params['flow_iters'],
                                     return_same_structure=True,
                                     name='flow-calculation')

                # inflow = tf.sparse_reduce_sum(flow, axis=0)
                # inflow = tf.expand_dims(inflow, axis=1)
                # adjusted_inflow = tf.nn.relu(inflow - demands)
                # flow = flow_weight_pred * adjusted_inflow

                # flow = tf.sparse.to_dense(flow)
                flow_cost = tf.reduce_sum(self.cost_fn.apply(flow.values))
                # flow_cost = tf.sparse.reduce_sum(flow)

                # flow = tf.sparse.to_dense(flow)

                # self.loss_op = tf.reduce_sum(tf.sparse.to_dense(flow_weight_pred))
                self.loss = tf.sparse.reduce_sum(flow_weight_pred, axis=1)
                self.loss_op = flow_cost
                self.output_ops += [flow_cost, flow, flow_weight_pred]
                self.optimizer_op = self._build_optimizer_op()
