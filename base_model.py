import tensorflow as tf
import numpy as np
from os.path import exists
from os import mkdir
from constants import *


class Model:

    def __init__(self, name, params):
        self.name = name
        self.params = params
        self._sess = tf.Session(graph=tf.Graph())
        #self.optimizer = tf.train.AdamOptimizer(learning_rate=params['learning_rate'])
        self.optimizer = tf.train.MomentumOptimizer(learning_rate=params['learning_rate'],
                                                    momentum=0.9)

        # Must be set by a concrete subclass
        self.loss_op = None
        self.optimizer_op = None
        self.output_ops = []

    def init(self):
        with self._sess.graph.as_default():
            init_op = tf.global_variables_initializer()
            self._sess.run(init_op)

    def build(self, inputs, **kwargs):
        raise NotImplementedError()

    def run_train_step(self, feed_dict):
        raise NotImplementedError()

    def inference(self, feed_dict):
        with self._sess.graph.as_default():
            op_results = self._sess.run(self.output_ops, feed_dict=feed_dict)
            return op_results

    def apply_gradients(self, gradients, variables, multiplier=1):
        clipped_grad, _ = tf.clip_by_global_norm(gradients, self.params['gradient_clip'])
        pruned_gradients = []
        for grad, var in zip(clipped_grad, variables):
            if grad is not None:
                pruned_gradients.append((multiplier * grad, var))

        return self.optimizer.apply_gradients(pruned_gradients)

    def save(self):
        out_folder = self.params['output_folder']
        if not exists(out_folder):
            mkdir(save_folder)

        params_path = PARAMS_FILE.format(out_folder)
        with gzip.GzipFile(params_path, 'wb') as out_file:
            pickle.dump(self.params, out_file)

        with self._sess.graph.as_default():
            model_path = MODEL_FILE.format(out_folder, self.name)
            saver = tf.train.Saver()
            saver.save(self._sess, model_path)

    def restore(self, save_folder):
        params_path = PARAMS_FILE.format(save_folder)
        with gzip.GzipFile(params_path, 'rb') as params_file:
            params_dict = pickle.load(params_file)

        self.params = params_dict

        with self._sess.graph.as_default():
            model_path = MODEL_FILE.format(save_folder, self.name)
            saver = tf.train.Saver()
            saver.restore(self._sess, model_path)
