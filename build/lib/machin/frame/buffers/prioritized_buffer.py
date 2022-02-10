from typing import Union, Dict, List, Tuple, Any
from ..transition import TransitionBase
from .buffer import Buffer
import torch as t
import numpy as np


class WeightTree:
    """
    Sum weight tree data structure.
    """

    def __init__(self, size: int):
        """
        Initialize a weight tree.

        Note:
            Weights must be positive.

        Note:
            Weight tree is stored as a flattened, full binary tree in a
            ``np.ndarray``. The lowest level of leaves comes first, the
            highest root node is stored at last.

            Example:

            Tree with weights: ``[[1, 2, 3, 4], [3, 7], [11]]``

            will be stored as: ``[1, 2, 3, 4, 3, 7, 11]``

        Note:
            Performance On i7-6700HQ (M: Million):

            90ms for building a tree with 10M elements.

            230ms for looking up 10M elements in a tree with 10M elements.

            20ms for 1M element batched update in a tree with 10M elements.

            240ms for 1M element single update in a tree with 10M elements.

        Args:
            size: Number of weight tree leaves.
        """
        self.size = size
        # depth: level of nodes
        self.max_leaf = 0
        self.depth = int(np.ceil(np.log2(self.size))) + 1
        level_sizes_log = np.arange(self.depth - 1, -1, -1)
        self.sizes = np.power(2, level_sizes_log)
        self.offsets = np.concatenate(([0], np.cumsum(self.sizes)))
        self.weights = np.zeros([self.offsets[-1]], dtype=np.float64)

    def get_weight_sum(self) -> float:
        """
        Returns:
            Total weight sum.
        """
        return self.weights[-1]

    def get_leaf_max(self) -> float:
        """
        Returns:
            Current maximum leaf weight.
        """
        return self.max_leaf

    def get_leaf_all_weights(self) -> np.ndarray:
        """
        Returns:
            Current weights of all leaves, ``np.ndarray`` of shape ``(size)``.
        """
        return self.weights[: self.size]

    def get_leaf_weight(self, index: Union[int, List[int], np.ndarray]) -> Any:
        """
        Get weights of selected leaves.

        Args:
            index: Leaf indexes in range ``[0, size - 1]``,
                used to query weights.

        Returns:
            Current weight(s) of selected leaves. If index is scalar, returns
            ``float``, if not, returns ``np.ndarray``.
        """
        if not isinstance(index, np.ndarray):
            index = np.array(index).reshape(-1)
        if np.any(index >= self.size) or np.any(index < 0):
            raise ValueError("Index has elements out of boundary!")
        if index.shape[0] == 1:
            return float(self.weights[index])
        else:
            return self.weights[index]

    def find_leaf_index(self, weight: Union[float, List[float], np.ndarray]):
        """
        Find leaf indexes given weight. Weight must be in range
        :math:`[0, weight\\_sum]`

        Args:
            weight: Weight(s) used to query leaf index(es).

        Returns:
            Leaf index(es), if weight is scalar, returns ``int``, if not,
            returns ``np.ndarray``.
        """
        if not isinstance(weight, np.ndarray):
            weight = np.array(weight).reshape(-1)
        index = np.zeros([weight.shape[0]], dtype=np.int64)

        # starting from the first child level of root
        for i in range(self.depth - 2, -1, -1):
            offset = self.offsets[i]
            left_wt = self.weights[offset + index * 2]
            # 0 for left and 1 for right
            select = weight > left_wt
            index = index * 2 + select
            weight = weight - left_wt * select
        index = np.clip(index, 0, self.size - 1)

        if index.shape[0] == 1:
            return int(index)
        else:
            return index

    def update_leaf(self, weight: float, index: int):
        """
        Update a single weight tree leaf.

        Args:
            weight: New weight of the leaf.
            index: Leaf index to update, must be in range ``[0, size - 1]``.
        """
        if not 0 <= index <= self.size:
            raise ValueError("Index has elements out of boundary!")
        self.max_leaf = max(weight, self.max_leaf)
        self.weights[index] = weight
        value = weight
        comp_value = self.weights[index ^ 0b1]

        for i in range(1, self.depth - 1):
            offset = self.offsets[i]
            index = index // 2
            global_index = index + offset
            comp_global_index = index ^ 0b1 + offset
            value = self.weights[global_index] = value + comp_value
            comp_value = self.weights[comp_global_index]

        self.weights[-1] = value + comp_value

    def update_leaf_batch(
        self,
        weights: Union[List[float], np.ndarray],
        indexes: Union[List[int], np.ndarray],
    ):
        """
        Update weight tree leaves in batch.

        Args:
            weights: New weights of leaves.
            indexes: Leaf indexes to update, must be in range ``[0, size - 1]``.
        """
        if len(weights) != len(indexes):
            raise ValueError("Dimension of weights and indexes doesn't match!")

        if len(weights) == 0:
            return

        weights = np.array(weights)
        indexes = np.array(indexes)
        if np.any(indexes >= self.size) or np.any(indexes < 0):
            raise ValueError("Index has elements out of boundary!")

        self.max_leaf = max(np.max(weights), self.max_leaf)

        needs_update = indexes
        self.weights[indexes] = weights
        for i in range(1, self.depth):
            offset, prev_offset = self.offsets[i], self.offsets[i - 1]
            # O(n) = nlg(n)
            needs_update = np.unique(needs_update // 2)
            tmp = needs_update * 2
            self.weights[offset + needs_update] = (
                self.weights[prev_offset + tmp] + self.weights[prev_offset + tmp + 1]
            )

    def update_all_leaves(self, weights: Union[List[float], np.ndarray]):
        """
        Reset all leaf weights, rebuild weight tree from ground up.

        Args:
            weights: All leaf weights. List or array length should be in range
                ``[0, size]``.
        """
        if len(weights) != self.size:
            raise ValueError("Weights size must match tree size!")
        self.weights[0 : len(weights)] = np.array(weights)
        self._build()

    def print_weights(self, precision=2):
        """
        Pretty print the tree, for debug purpose.

        Args:
            precision: Number of digits of weights to print.
        """
        fmt = f"{{:.{precision}f}}"
        for i in range(self.depth):
            offset, size = self.offsets[i], self.sizes[i]
            weights = [
                fmt.format(self.weights[j]) for j in range(offset, offset + size)
            ]
            print(weights)

    def _build(self):
        """
        Build weight tree from leaves
        """
        self.max_leaf = np.max(self.get_leaf_all_weights())
        for i in range(self.depth - 1):
            offset = self.offsets[i]
            level_size = self.sizes[i]
            # data are interleaved, therefore must be -1,2 and not 2,-1
            weight_sum = (
                self.weights[offset : offset + level_size].reshape(-1, 2).sum(axis=1)
            )

            offset += level_size
            next_level_size = self.sizes[i + 1]
            self.weights[offset : offset + next_level_size] = weight_sum


class PrioritizedBuffer(Buffer):
    def __init__(
        self,
        buffer_size: int = 1000000,
        buffer_device: Union[str, t.device] = "cpu",
        epsilon: float = 1e-2,
        alpha: float = 0.6,
        beta: float = 0.4,
        beta_increment_per_sampling: float = 0.001,
        **kwargs,
    ):
        """
        Note:
            `PrioritizedBuffer` does not support customizing storage as it
            requires a linear storage.

        Args:
            buffer_size: Maximum buffer size.
            buffer_device: Device where buffer is stored.
            epsilon: A small positive constant used to prevent edge-case
                zero weight transitions from never being visited.
            alpha: Prioritization weight. Used during transition sampling:
                :math:`j \\sim P(j)=p_{j}^{\\alpha} / \
                        \\sum_i p_{i}^{\\alpha}`.
                When ``alpha = 0``, all samples have the same probability
                to be sampled.
                When ``alpha = 1``, all samples are drawn uniformly according
                to their weight.
            beta: Bias correcting weight. When ``beta = 1``, bias introduced
                by prioritized replay will be corrected. Used during
                importance weight calculation:
                :math:`w_j=(N \\cdot P(j))^{-\\beta}/max_i w_i`
            beta_increment_per_sampling:
                Beta increase step size, will gradually increase ``beta`` to 1.
        """
        super().__init__(
            buffer_size=buffer_size, buffer_device=buffer_device, storage=None, **kwargs
        )
        self.epsilon = epsilon
        self.alpha = alpha
        self.beta = beta
        self.beta_increment_per_sampling = beta_increment_per_sampling
        self.curr_beta = beta
        self.wt_tree = WeightTree(buffer_size)

    def store_episode(
        self,
        episode: List[Union[TransitionBase, Dict]],
        priorities: Union[List[float], None] = None,
        required_attrs=("state", "action", "next_state", "reward", "terminal"),
    ):
        """
        Store an episode to the buffer.

        Args:
            episode: A list of transition objects.
            priorities: Priority of each transition in the episode.
            required_attrs: Required attributes. Could be an empty tuple if
                no attribute is required.

        Raises:
            ``ValueError`` if episode is empty.
            ``ValueError`` if any transition object in the episode doesn't have
            required attributes in ``required_attrs``.
        """
        super().store_episode(episode, required_attrs)
        episode_number = self.episode_counter - 1
        positions = self.episode_transition_handles[episode_number]
        if priorities is None:
            # the initialization method used in the original essay
            priority = self._normalize_priority(self.wt_tree.get_leaf_max())
            self.wt_tree.update_leaf_batch([priority] * len(positions), positions)
        else:
            self.wt_tree.update_leaf_batch(
                self._normalize_priority(priorities), positions
            )

    def clear(self):
        """
        Clear and resets the buffer to its initial state.
        """
        super().clear()
        self.wt_tree = WeightTree(self.storage.max_size)
        self.curr_beta = self.beta

    def update_priority(self, priorities: np.ndarray, indexes: np.ndarray):
        """
        Update priorities of samples.

        Args:
            priorities: New priorities.
            indexes: Indexes of samples, returned by :meth:`sample_batch`
        """
        priorities = self._normalize_priority(priorities)
        self.wt_tree.update_leaf_batch(priorities, indexes)

    def sample_batch(
        self,
        batch_size: int,
        concatenate: bool = True,
        device: Union[str, t.device] = "cpu",
        sample_attrs: List[str] = None,
        additional_concat_custom_attrs: List[str] = None,
        *_,
        **__,
    ) -> Tuple[
        int, Union[None, tuple], Union[None, np.ndarray], Union[None, np.ndarray]
    ]:
        """
        Sample the most important batch from the prioritized buffer.

        See Also:
             :meth:`.Buffer.sample_batch`

        Args:
            batch_size: A hint size of the result sample.
            concatenate: Whether perform concatenation on major, sub and custom
                         attributes.
                         If ``True``, for each value in dictionaries of major
                         attributes. and each value of sub attributes, returns
                         a concatenated tensor. Custom Attributes specified in
                         ``additional_concat_custom_attrs`` will also be concatenated.
                         If ``False``, performs no concatenation.
            device:      Device to move tensors in the batch to.
            sample_attrs: If sample_keys is specified, then only specified keys
                         of the transition object will be sampled. You may use
                         ``"*"`` as a wildcard to collect remaining
                         **custom keys** as a ``dict``, you cannot collect major
                         and sub attributes using this.
                         Invalid sample attributes will be ignored.
            additional_concat_custom_attrs: additional **custom keys** needed to be
                         concatenated, will only work if ``concatenate`` is
                         ``True``.

        Returns:
            1. Batch size.

            2. Sampled attribute values in the same order as ``sample_keys``.

               Sampled attribute values is a tuple. Or ``None`` if sampled
               batch size is zero (E.g.: if buffer is empty or your sample
               size is 0).

               For details on the attribute values, please refer to doc of
               :meth:`.Buffer.sample_batch`.

            3. Indexes of samples in the weight tree, ``None`` if sampled
               batch size is zero.

            4. Importance sampling weight of samples, ``None`` if sampled
               batch size is zero.

        """
        if batch_size <= 0 or self.size() == 0:
            return 0, None, None, None

        index, is_weight = self.sample_index_and_weight(batch_size)
        batch = [self.storage[idx] for idx in index]
        result = self.post_process_batch(
            batch, device, concatenate, sample_attrs, additional_concat_custom_attrs
        )
        return len(batch), result, index, is_weight

    def sample_index_and_weight(self, batch_size: int, all_weight_sum: float = None):
        """
        Sample index of experience entries by priority, and return their importance
        sampling weight.

        Args:
            batch_size: Batch size to sample.
            all_weight_sum: Sum of all weights from all agents,
                used by the distributed version.
        Returns:
            Index array and importance sampling weight array.
        """
        segment_length = self.wt_tree.get_weight_sum() / batch_size

        rand_priority = np.random.uniform(size=batch_size) * segment_length
        rand_priority += np.arange(batch_size, dtype=np.float64) * segment_length
        rand_priority = np.clip(
            rand_priority, 0, max(self.wt_tree.get_weight_sum() - 1e-6, 0)
        )
        index = self.wt_tree.find_leaf_index(rand_priority)

        priority = self.wt_tree.get_leaf_weight(index)

        # calculate importance sampling weight
        all_weight_sum = all_weight_sum or self.wt_tree.get_weight_sum()
        sample_probability = priority / all_weight_sum
        is_weight = np.power(len(self.storage) * sample_probability, -self.curr_beta)
        is_weight /= is_weight.max()
        self.curr_beta = np.min(
            [1.0, self.curr_beta + self.beta_increment_per_sampling]
        )
        return index, is_weight

    def _normalize_priority(self, priority):
        """
        Normalize priority and calculate :math:`p_{j}^{\alpha}`
        """
        return (np.abs(priority) + self.epsilon) ** self.alpha
