from computation_graphs.computation_graph import computation_graph
from recordclass import recordclass

import tensorflow as tf
import numpy as np


class ComputationGraph(computation_graph.ComputationGraph):
    Transformation = int
    Node = int

    Transformation_ = recordclass(
        'Transformation', 'inputs input_gradients '
                          'output output_gradient '
                          'model_gradient_accumulators '
                          'zero_model_gradient_accumulators '
                          'update_model_gradient_accumulators '
                          'seed training')
    Node_ = recordclass('Node', 'transformation inputs output')
    Batch = recordclass('Batch', 'nodes_end seeds')

    def __init__(self, training: bool, session: tf.Session):
        super().__init__(training)
        self.session = session
        self.transformations = []
        self.nodes = []
        self.batches = [ComputationGraph.Batch(nodes_end=0, seeds=None)]
        self.nodes_shift = 0

    def transformation(
            self, inputs: [tf.Tensor], input_gradients: [tf.Tensor],
            output: tf.Tensor, output_gradient: tf.Tensor,
            model_gradient_accumulators: [tf.Tensor],
            zero_model_gradient_accumulators,
            update_model_gradient_accumulators,
            seed: tf.Tensor, training: tf.Tensor) \
            -> Transformation:
        self.transformations += [
            ComputationGraph.Transformation_(
                inputs=inputs,
                input_gradients=input_gradients,
                output=output,
                output_gradient=output_gradient,
                model_gradient_accumulators=model_gradient_accumulators,
                zero_model_gradient_accumulators=
                zero_model_gradient_accumulators,
                update_model_gradient_accumulators=
                update_model_gradient_accumulators,
                seed=seed,
                training=training)]
        return len(self.transformations) - 1

    def matrix(self, matrix: np.ndarray) -> Node:
        self.nodes += [
            ComputationGraph.Node_(
                transformation=None,
                inputs=None,
                output=matrix)]
        return self.nodes_shift + len(self.nodes) - 1

    # inputs: Union [Node] [[Node]]
    def transformation_run(self, transformation: Node, inputs) -> Node:
        self.nodes += [
            ComputationGraph.Node_(
                transformation=transformation,
                inputs=inputs,
                output=None)]
        return self.nodes_shift + len(self.nodes) - 1

    def __input(self, node_input, tensor: tf.Tensor):
        if type(node_input) == list:
            concatenated_input = np.concatenate(
                [self.nodes[node_input_ - self.nodes_shift].output
                 for node_input_ in node_input])
            expanded_input = np.lib.pad(
                concatenated_input,
                (0, tensor.get_shape()[-1].value -
                    concatenated_input.shape[0]),
                'constant')
            return expanded_input
        else:
            return self.nodes[node_input - self.nodes_shift].output

    def run_batch(self):
        seeds = [
            np.random.randint(
                np.iinfo(np.int64).max,
                size=transformation.seed.get_shape()[0].value,
                dtype=np.int64)
            for transformation in self.transformations]

        self.batches += [
            ComputationGraph.Batch(
                nodes_end=self.nodes_shift + len(self.nodes),
                seeds=seeds)]

        tasks_inputs = [
            [[] for _ in transformation.inputs]
            for transformation in self.transformations]

        for node_index in range(
                self.batches[-2].nodes_end, self.batches[-1].nodes_end):
            node = self.nodes[node_index - self.nodes_shift]
            if node.transformation is not None:
                transformation = self.transformations[node.transformation]
                task_inputs = tasks_inputs[node.transformation]
                for task_input, node_input, transformation_input in zip(
                        task_inputs, node.inputs, transformation.inputs):
                    task_input += [
                        self.__input(node_input, transformation_input)]

        fetches = {
            i: transformation.output
            for i, (transformation, task_inputs) in enumerate(
                zip(self.transformations, tasks_inputs))
            if task_inputs[0] != []}

        seed_feed_dict = {
            transformation.seed: seed
            for transformation, seed in zip(self.transformations, seeds)}
        training_feed_dict = {
            transformation.training: self.training
            for transformation in self.transformations}
        input_feed_dict = {
            input: value
            for transformation, task_inputs in zip(
                self.transformations, tasks_inputs)
            if task_inputs[0] != []
            for input, value in zip(transformation.inputs, task_inputs)}

        results = self.session.run(
            fetches=fetches,
            feed_dict={
                **seed_feed_dict,
                **training_feed_dict,
                **input_feed_dict})

        for node_index in range(
                self.batches[-2].nodes_end, self.batches[-1].nodes_end):
            node = self.nodes[node_index - self.nodes_shift]
            if node.transformation is not None:
                node.output, results[node.transformation] = \
                    results[node.transformation][0], \
                    results[node.transformation][1:]

    def value(self, node_index: Node) -> np.ndarray:
        return self.nodes[node_index - self.nodes_shift].output

    def __propagate_input_gradient(
            self, node_input, input_gradient, gradients, gradients_shift):
        if type(node_input) == list:
            for node_input_ in node_input:
                node = self.nodes[node_input_ - self.nodes_shift]
                length = node.output.shape[0]

                if node_input_ - gradients_shift >= 0:
                    gradients[node_input_ - gradients_shift] \
                        += input_gradient[:length]

                input_gradient = input_gradient[length:]
        else:
            if node_input - gradients_shift >= 0:
                gradients[node_input - gradients_shift] += input_gradient

    def model_gradients(self, first_node: Node, y_grads):
        for transformation in self.transformations:
            self.session.run(transformation.zero_model_gradient_accumulators)

        gradients_shift = first_node

        gradients = [
            np.zeros(
                shape=self.nodes[node_index - self.nodes_shift].output.shape,
                dtype=self.nodes[node_index - self.nodes_shift].output.dtype)
            for node_index in range(
                first_node, self.nodes_shift + len(self.nodes))]

        for node_index, gradient in y_grads.items():
            gradients[node_index - gradients_shift] = gradient

        batches = list(self.batches)

        while len(batches) > 1 and first_node < batches[-1].nodes_end:
            tasks_inputs = [
                [[] for _ in transformation.inputs]
                for transformation in self.transformations]

            for node_index in range(
                    batches[-2].nodes_end, batches[-1].nodes_end):
                node = self.nodes[node_index - self.nodes_shift]
                if node.transformation is not None:
                    transformation = self.transformations[node.transformation]
                    task_inputs = tasks_inputs[node.transformation]
                    for task_input, node_input, transformation_input in zip(
                            task_inputs, node.inputs, transformation.inputs):
                        task_input += [self.__input(
                            node_input, transformation_input)]

            tasks_output_gradient = [[] for _ in self.transformations]

            for node_index in range(
                    batches[-2].nodes_end, batches[-1].nodes_end):
                node = self.nodes[node_index - self.nodes_shift]
                if node.transformation is not None:
                    tasks_output_gradient[node.transformation] += [
                        gradients[node_index - gradients_shift]]

            update_model_gradient_accumulators = [
                transformation.update_model_gradient_accumulators
                for transformation, task_inputs in zip(
                    self.transformations, tasks_inputs)
                if task_inputs[0] != []]
            input_gradients_fetches = {
                transformation_index: [
                    input_gradient if input_gradient is not None else []
                    for input_gradient in transformation.input_gradients]
                for transformation_index, (transformation, task_inputs) in
                enumerate(zip(self.transformations, tasks_inputs))
                if task_inputs[0] != []}

            seed_feed_dict = {
                transformation.seed: seed
                for transformation, seed, task_inputs in zip(
                    self.transformations, batches[-1].seeds, tasks_inputs)
                if task_inputs[0] != []}
            training_feed_dict = {
                transformation.training: self.training
                for transformation, task_inputs in zip(
                    self.transformations, tasks_inputs)
                if task_inputs[0] != []}
            input_feed_dict = {
                input: value
                for transformation, task_inputs in zip(
                    self.transformations, tasks_inputs)
                if task_inputs[0] != []
                for input, value in zip(transformation.inputs, task_inputs)}
            output_gradient_feed_dict = {
                transformation.output_gradient: task_output_gradient
                for transformation, task_output_gradient in zip(
                    self.transformations, tasks_output_gradient)
                if task_output_gradient != []}

            results = self.session.run(
                fetches={
                    'update_model_gradient_accumulators':
                        update_model_gradient_accumulators,
                    'input_gradients': input_gradients_fetches},
                feed_dict={
                    **seed_feed_dict,
                    **training_feed_dict,
                    **input_feed_dict,
                    **output_gradient_feed_dict})

            for node_index in range(
                    batches[-2].nodes_end, batches[-1].nodes_end):
                node = self.nodes[node_index - self.nodes_shift]

                if node.transformation is not None:
                    transformation_input_gradients = \
                        results['input_gradients'][node.transformation]

                    for input_index, node_input in enumerate(node.inputs):
                        if transformation_input_gradients[input_index] != []:
                            input_gradient = \
                                transformation_input_gradients[input_index][0]
                            transformation_input_gradients[input_index] = \
                                transformation_input_gradients[input_index][1:]
                            self.__propagate_input_gradient(
                                node_input, input_gradient, gradients,
                                gradients_shift)

            batches.pop()

        return [
            self.session.run(transformation.model_gradient_accumulators)
            for transformation in self.transformations]

    def shift(self, nodes_shift):
        first_batch_index = next(
            i
            for i, batch in enumerate(self.batches)
            if nodes_shift <= batch.nodes_end)
        self.batches = self.batches[first_batch_index:]

        self.nodes = self.nodes[nodes_shift - self.nodes_shift:]
        self.nodes_shift = nodes_shift