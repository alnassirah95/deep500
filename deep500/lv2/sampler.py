""" Implements classes and methods related to sampling datasets. """

import numpy as np
import random
from typing import Any, Callable, Dict, List
from collections import Iterator
from deep500.lv2.dataset import Dataset
from deep500.lv2.event import SamplerEvent

Distribution = List[float]


class Sampler(Iterator):
    """ Base sampler class. """

    def __init__(
            self,
            dataset: Dataset,
            batch_size: int,
            seed: int = None,
            drop_last_batch: bool = True,
            events: List[SamplerEvent] = None,
            transformations: Callable[[Dict[str, Any]], Dict[str, Any]] = None
    ):
        self.dataset = dataset
        self.batch_size = batch_size
        self.seed = seed
        self.drop_last_batch = drop_last_batch
        self.events = events or []
        self.random_state = None
        self.transformations = transformations or []
        for transform in self.transformations:
            if hasattr(transform, 'set_dataset_nodes'):
                transform.set_dataset_nodes(self.dataset.input_node,
                                            self.dataset.label_node)
        self.reset()

    def as_operator(self):
        """ Returns a CustomOperator that generates the input and (optionally)
            label, to streamline data serving.
        """
        raise NotImplementedError

    def __iter__(self):
        return self

    def __next__(self):
        raise NotImplementedError()
        
    def __call__(self):
        return self.__next__()

    def __len__(self):
        """ Defines the length of an epoch, or 0 for running until a 
            StopIteration exeption is raised. """
        return len(self.dataset) // self.batch_size

    def reset(self):
        for event in self.events: event.on_sampler_reset(self)
        self.random_state = np.random.RandomState(self.seed)

    def add_transformation(self, transform: Callable[[Dict[str, Any]], Any]):
        """ Apply a transformation (e.g., data augmentation) on an input after
            it is sampled.
            @param transform: The transformation to apply on a minibatch.
        """
        if hasattr(transform, 'set_dataset_nodes'):
            transform.set_dataset_nodes(self.dataset.input_node,
                                        self.dataset.label_node)
        self.transformations.append(transform)

    def _transform(self, batch):
        for transform in self.transformations:
            batch = transform(batch)
        # if len(self.transformations) == 0:
        #     return batch
        # return random.choice(self.transformations)(batch)
        return batch


class OrderedSampler(Sampler):
    """ The OrderedSampler samples the dataset in a sequential order. """

    def __init__(
            self,
            dataset: Dataset,
            batch_size: int,
            drop_last_batch: bool = True,
            events: List[SamplerEvent] = None,
            transformations: Callable[[Dict[str, Any]], Dict[str, Any]] = None
    ):
        super().__init__(dataset, batch_size, 0, drop_last_batch, events,
                         transformations)
        self.batch_idx = 0

    def __next__(self):
        for event in self.events: event.before_sampling(self, self.batch_size)
        if (self.drop_last_batch and
                self.batch_idx + self.batch_size > len(self.dataset)):
            raise StopIteration
        if self.batch_idx >= len(self.dataset):
            raise StopIteration
        batch = self.dataset[self.batch_idx:
                             min(self.batch_idx + self.batch_size,
                                 len(self.dataset))]
        batch = self._transform(batch)
        self.batch_idx += self.batch_size

        for event in self.events: event.after_sampling(self, batch)
        return batch

    def reset(self):
        super().reset()
        self.batch_idx = 0


class ShuffleSampler(Sampler):
    """ The ShuffleSampler class approximates the uniform distribution. On
        initialization and every reset, it shuffles the list of samples. On
        each next call, it returns a continuous chunk of samples. """

    def __init__(
            self,
            dataset: Dataset,
            batch_size: int,
            seed: int = None,
            drop_last_batch: bool = True,
            events: List[SamplerEvent] = None,
            transformations: Callable[[Dict[str, Any]], Dict[str, Any]] = None
    ):
        self.sample_pool = np.arange(len(dataset))
        self.batch_idx = 0
        super().__init__(dataset, batch_size, seed, drop_last_batch,
                         events, transformations)

    def __next__(self):
        for event in self.events: event.before_sampling(self, self.batch_size)
        if (self.drop_last_batch and
                self.batch_idx + self.batch_size > len(self.dataset)):
            raise StopIteration
        if self.batch_idx >= len(self.dataset):
            raise StopIteration
        batch = self.dataset[self.sample_pool[self.batch_idx:min(
            self.batch_idx + self.batch_size, len(self.dataset))]]
        batch = self._transform(batch)
        self.batch_idx += self.batch_size

        for event in self.events: event.after_sampling(self, batch)
        return batch

    def reset(self):
        super().reset()
        if self.dataset is not None:
            self.random_state.shuffle(self.sample_pool)
        self.batch_idx = 0


class ChoiceSampler(Sampler):
    """ The ChoiceSampler class approximates any distribution, given as a list
    of floats, one for each sample, that represent the probability of picking
    each one of them. By default, it approximates the uniform distribution.
    Other distributions may be used if sampling with replacement is enabled. """

    def __init__(
            self,
            dataset: Dataset,
            batch_size: int,
            seed: int = None,
            drop_last_batch: bool = True,
            replacement: bool = False,
            num_batches: int = -1,
            distribution: Distribution = None,
            events: List[SamplerEvent] = None,
            transformations: Callable[[Dict[str, Any]], Dict[str, Any]] = None
    ):
        self.replacement = replacement
        self.num_batches = num_batches
        self.distribution = distribution
        self.sample_pool = dataset
        self.batch_idx = 0
        super().__init__(dataset, batch_size, seed, drop_last_batch, events,
                         transformations)

    def __next__(self):
        for event in self.events: event.before_sampling(self, self.batch_size)

        # With replacement
        if self.replacement:
            if self.batch_idx >= self.num_batches:
                raise StopIteration
            self.batch_idx += 1
            return list(self.random_state.choice(
                self.dataset, size=self.batch_size,
                replace=self.replacement, p=self.distribution
            ))
        # Without replacement
        batch_size = min(self.batch_size, len(self.sample_pool))
        if ((self.drop_last_batch and batch_size < self.batch_size) or
                batch_size == 0):
            raise StopIteration
        batch = list(self.random_state.choice(
            self.sample_pool, size=batch_size, replace=self.replacement
        ))
        batch = self._transform(batch)
        self.sample_pool = [sample for sample in self.sample_pool
                            if sample not in batch]

        for event in self.events: event.after_sampling(self, batch)
        return batch

    def reset(self):
        super().reset()
        self.sample_pool = self.dataset
        self.batch_idx = 0

