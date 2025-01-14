from typing import Union, Dict, List, Tuple, Callable, Any
from copy import deepcopy
import torch as t
import torch.nn as nn
import numpy as np
import random
from machin.frame.buffers.buffer import Buffer
#from machin.frame.transition import Transition
from torchvision import transforms
from machin.model.nets.base import NeuralNetworkModule
from collections import namedtuple
from .base import TorchFramework, Config
from .utils import (
    hard_update,
    soft_update,
    safe_call,
    assert_and_get_valid_models,
    assert_and_get_valid_optimizer,
    assert_and_get_valid_criterion,
    assert_and_get_valid_lr_scheduler,
)
from torch.nn.functional import smooth_l1_loss
from numpy.random import choice


Transition_n = namedtuple('Transition', ('state', 'action', 'reward', 'next_state'))
Transition= namedtuple('Transition', ('state', 'action', 'reward', 'next_state'))

class ReplayBuffer:
    """Fixed-size buffer to store experience tuples."""

    def __init__(self,  buffer_size, batch_size, experiences_per_sampling, seed, compute_weights):
        """Initialize a ReplayBuffer object.
        Params
        ======
            action_size (int): dimension of each action
            buffer_size (int): maximum size of buffer
            experiences_per_sampling (int): number of experiences to sample during a sampling iteration
            batch_size (int): size of each training batch
            seed (int): random seed
        """
        self.buffer_size = buffer_size
        self.batch_size = batch_size
        self.experiences_per_sampling = experiences_per_sampling
        
        self.alpha = 0.5
        self.alpha_decay_rate = 0.99
        self.beta = 0.5
        self.beta_growth_rate = 1.001
        self.seed = random.seed(seed)
        self.compute_weights = compute_weights
        self.experience_count = 0
        
        self.experience = namedtuple("Experience", 
            field_names=["state", "action", "reward", "next_state", "done"])
        self.data = namedtuple("Data", 
            field_names=["priority", "probability", "weight","index"])

        indexes = []
        datas = []
        for i in range(buffer_size):
            indexes.append(i)
            d = self.data(0,0,0,i)
            datas.append(d)
        
        self.memory = {key: self.experience for key in indexes}
        self.memory_data = {key: data for key,data in zip(indexes, datas)}
        self.sampled_batches = []
        self.current_batch = 0
        self.priorities_sum_alpha = 0
        self.priorities_max = 1
        self.weights_max = 1
    
    def update_priorities(self, tds, indices):
        for td, index in zip(tds, indices):
            N = min(self.experience_count, self.buffer_size)

            updated_priority = td
            if updated_priority > self.priorities_max:
                self.priorities_max = updated_priority
            
            if self.compute_weights:
                updated_weight = ((N * updated_priority)**(-self.beta))/self.weights_max
                if updated_weight > self.weights_max:
                    self.weights_max = updated_weight
            else:
                updated_weight = 1

            old_priority = self.memory_data[index].priority
            self.priorities_sum_alpha += updated_priority**self.alpha - old_priority**self.alpha
            updated_probability = td**self.alpha / self.priorities_sum_alpha
            data = self.data(updated_priority, updated_probability, updated_weight, index) 
            self.memory_data[index] = data

    def update_memory_sampling(self):
        """Randomly sample X batches of experiences from memory."""
        # X is the number of steps before updating memory
        self.current_batch = 0
        values = list(self.memory_data.values())
        random_values = random.choices(self.memory_data, 
                                       [data.probability for data in values], 
                                       k=self.experiences_per_sampling)
        self.sampled_batches = [random_values[i:i + self.batch_size] 
                                    for i in range(0, len(random_values), self.batch_size)]

    def update_parameters(self):
        self.alpha *= self.alpha_decay_rate
        self.beta *= self.beta_growth_rate
        if self.beta > 1:
            self.beta = 1
        N = min(self.experience_count, self.buffer_size)
        self.priorities_sum_alpha = 0
        sum_prob_before = 0
        for element in self.memory_data.values():
            sum_prob_before += element.probability
            self.priorities_sum_alpha += element.priority**self.alpha
        sum_prob_after = 0
        for element in self.memory_data.values():
            probability = element.priority**self.alpha / self.priorities_sum_alpha
            sum_prob_after += probability
            weight = 1
            if self.compute_weights:
                weight = ((N *  element.probability)**(-self.beta))/self.weights_max
            d = self.data(element.priority, probability, weight, element.index)
            self.memory_data[element.index] = d
        print("sum_prob before", sum_prob_before)
        print("sum_prob after : ", sum_prob_after)
    
    def add(self, state, action, reward, next_state, done):
        """Add a new experience to memory."""
        self.experience_count += 1
        index = self.experience_count % self.buffer_size

        if self.experience_count > self.buffer_size:
            temp = self.memory_data[index]
            self.priorities_sum_alpha -= temp.priority**self.alpha
            if temp.priority == self.priorities_max:
                self.memory_data[index].priority = 0
                self.priorities_max = max(self.memory_data.items(), key=operator.itemgetter(1)).priority
            if self.compute_weights:
                if temp.weight == self.weights_max:
                    self.memory_data[index].weight = 0
                    self.weights_max = max(self.memory_data.items(), key=operator.itemgetter(2)).weight

        priority = self.priorities_max
        weight = self.weights_max
        self.priorities_sum_alpha += priority ** self.alpha
        probability = priority ** self.alpha / self.priorities_sum_alpha
        e = self.experience(state, action, reward, next_state, done)
        self.memory[index] = e
        d = self.data(priority, probability, weight, index)
        self.memory_data[index] = d
            
    def sample(self):
        sampled_batch = self.sampled_batches[self.current_batch]
        self.current_batch += 1
        experiences = []
        weights = []
        indices = []
        
        for data in sampled_batch:
            experiences.append(self.memory.get(data.index))
            weights.append(data.weight)
            indices.append(data.index)
        '''
        states = torch.from_numpy(
            np.vstack([e.state for e in experiences if e is not None])).float().to(device)
        actions = torch.from_numpy(
            np.vstack([e.action for e in experiences if e is not None])).long().to(device)
        rewards = torch.from_numpy(
            np.vstack([e.reward for e in experiences if e is not None])).float().to(device)
        next_states = torch.from_numpy(
            np.vstack([e.next_state for e in experiences if e is not None])).float().to(device)
        dones = torch.from_numpy(
            np.vstack([e.done for e in experiences if e is not None]).astype(np.uint8)).float().to(device)
        ''' 

        return self.experience(*zip(*experiences)),weights,indices
        #return (states, actions, rewards, next_states, dones, weights, indices)

    def __len__(self):
        """Return the current size of internal memory."""
        return len(self.memory)

class DQNPer(TorchFramework):
    """
    DQN framework.
    """

    _is_top = ["qnet", "qnet_target"]
    _is_restorable = ["qnet_target"]

    def __init__(
        self,
        qnet: Union[NeuralNetworkModule, nn.Module],
        qnet_target: Union[NeuralNetworkModule, nn.Module],
        optimizer: Callable,
        criterion: Callable,
        *_,
        lr_scheduler: Callable = None,
        lr_scheduler_args: Tuple[Tuple] = None,
        lr_scheduler_kwargs: Tuple[Dict] = None,
        batch_size: int = 100,
        epsilon_decay: float = 0.9999,
        update_rate: Union[float, None] = 0.005,
        update_steps: Union[int, None] = None,
        learning_rate: float = 0.001,
        discount: float = 0.99,
        gradient_max: float = np.inf,
        replay_size: int = 500000,
        replay_device: Union[str, t.device] = "cpu",
        replay_buffer: Buffer = None,
        mode: str = "double",
        visualize: bool = False,
        visualize_dir: str = "",
        momentum: float =  1, 
        weight_decay: float= 1,
        **__,
        
    ):
        """
        Note:
            DQN is only available for discrete environments.

        Note:
            Dueling DQN is a network structure rather than a framework, so
            it could be applied to all three modes.

            If ``mode = "vanilla"``, implements the simplest online DQN,
            with replay buffer.

            If ``mode = "fixed_target"``, implements DQN with a target network,
            and replay buffer. Described in `this <https://web.stanford.\
edu/class/psych209/Readings/MnihEtAlHassibis15NatureControlDeepRL.pdf>`__ essay.

            If ``mode = "double"``, implements Double DQN described in
            `this <https://arxiv.org/pdf/1509.06461.pdf>`__ essay.

        Note:
            Vanilla DQN only needs one network, so internally, ``qnet``
            is assigned to ``qnet_target``.

        Note:
            In order to implement dueling DQN, you should create two dense
            output layers.

            In your q network::

                    self.fc_adv = nn.Linear(in_features=...,
                                            out_features=num_actions)
                    self.fc_val = nn.Linear(in_features=...,
                                            out_features=1)

            Then in your ``forward()`` method, you should implement output as::

                    adv = self.fc_adv(some_input)
                    val = self.fc_val(some_input).expand(self.batch_sze,
                                                         self.num_actions)
                    return val + adv - adv.mean(1, keepdim=True)

        Note:
            Your optimizer will be called as::

                optimizer(network.parameters(), learning_rate)

            Your lr_scheduler will be called as::

                lr_scheduler(
                    optimizer,
                    *lr_scheduler_args[0],
                    **lr_scheduler_kwargs[0],
                )

            Your criterion will be called as::

                criterion(
                    target_value.view(batch_size, 1),
                    predicted_value.view(batch_size, 1)
                )

        Note:
            DQN supports two ways of updating the target network, the first
            way is polyak update (soft update), which updates the target network
            in every training step by mixing its weights with the online network
            using ``update_rate``.

            The other way is hard update, which copies weights of the online
            network after every ``update_steps`` training step.

            You can either specify ``update_rate`` or ``update_steps`` to select
            one update scheme, if both are specified, an error will be raised.

            These two different update schemes may result in different training
            stability.

        Attributes:
            epsilon: Current epsilon value, determines randomness in
                ``act_discrete_with_noise``. You can set it to any value.

        Args:
            qnet: Q network module.
            qnet_target: Target Q network module.
            optimizer: Optimizer used to optimize ``qnet``.
            criterion: Criterion used to evaluate the value loss.
            learning_rate: Learning rate of the optimizer, not compatible with
                ``lr_scheduler``.
            lr_scheduler: Learning rate scheduler of ``optimizer``.
            lr_scheduler_args: Arguments of the learning rate scheduler.
            lr_scheduler_kwargs: Keyword arguments of the learning
                rate scheduler.
            batch_size: Batch size used during training.
            epsilon_decay: Epsilon decay rate per acting with noise step.
                ``epsilon`` attribute is multiplied with this every time
                ``act_discrete_with_noise`` is called.
            update_rate: :math:`\\tau` used to update target networks.
                Target parameters are updated as:

                :math:`\\theta_t = \\theta * \\tau + \\theta_t * (1 - \\tau)`
            update_steps: Training step number used to update target networks.
            discount: :math:`\\gamma` used in the bellman function.
            gradient_max: Maximum gradient.
            replay_size: Replay buffer size. Not compatible with
                ``replay_buffer``.
            replay_device: Device where the replay buffer locates on, Not
                compatible with ``replay_buffer``.
            replay_buffer: Custom replay buffer.
            mode: one of ``"vanilla", "fixed_target", "double"``.
            visualize: Whether visualize the network flow in the first pass.
        """
        self.batch_size = batch_size
        self.epsilon_decay = epsilon_decay
        self.update_rate = update_rate
        self.update_steps = update_steps
        self.discount = discount
        self.grad_max = gradient_max
        self.visualize = visualize
        self.visualize_dir = visualize_dir
        self.mode = mode
        self.epsilon = 1
        self._update_counter = 0

        if mode not in {"vanilla", "fixed_target", "double"}:
            raise ValueError(f"Unknown DQN mode: {mode}")

        if update_rate is not None and update_steps is not None:
            raise ValueError(
                "You can only specify one target network update"
                " scheme, either by update_rate or update_steps,"
                " but not both."
            )

        self.qnet = qnet
        if self.mode == "vanilla":
            self.qnet_target = qnet
        else:
            self.qnet_target = qnet_target
        try: 
            self.qnet_optim = optimizer(self.qnet.parameters(), lr=learning_rate,momentum=momentum, weight_decay=weight_decay)
            print("success")
        except : 
            self.qnet_optim = optimizer(self.qnet.parameters(), lr=learning_rate)
            print("failure")
        
        self.replay_buffer = ReplayBuffer(replay_size,self.batch_size,160,0,False)

        # Make sure target and online networks have the same weight
        with t.no_grad():
            hard_update(self.qnet, self.qnet_target)

        if lr_scheduler is not None:
            if lr_scheduler_args is None:
                lr_scheduler_args = ((),)
            if lr_scheduler_kwargs is None:
                lr_scheduler_kwargs = ({},)
            self.qnet_lr_sch = lr_scheduler(
                self.qnet_optim, *lr_scheduler_args[0], **lr_scheduler_kwargs[0]
            )

        self.criterion = criterion

        super().__init__()

    @property
    def optimizers(self):
        return [self.qnet_optim]

    @optimizers.setter
    def optimizers(self, optimizers):
        self.qnet_optim = optimizers[0]

    @property
    def lr_schedulers(self):
        if hasattr(self, "qnet_lr_sch"):
            return [self.qnet_lr_sch]
        return []

    def act(self, state: Dict[str, Any], use_target: bool = False, **__):
            """
            Use Q network to produce a discrete action for
            the current state.

            Args:
                state: Current state.
                use_target: Whether to use the target network.

            Returns:
                Action of shape ``[batch_size, 1]``.
                Any other things returned by your Q network. if they exist.
            """
            if use_target:
                result, *others = safe_call(self.qnet_target, state)
            else:
                result, *others = safe_call(self.qnet, state)

            return result
    
    def act_discrete(self, state: Dict[str, Any], use_target: bool = False, **__):
        """
        Use Q network to produce a discrete action for
        the current state.

        Args:
            state: Current state.
            use_target: Whether to use the target network.

        Returns:
            Action of shape ``[batch_size, 1]``.
            Any other things returned by your Q network. if they exist.
        """
        if use_target:
            result, *others = safe_call(self.qnet_target, state)
        else:
            result, *others = safe_call(self.qnet, state)

        result = t.argmax(result, dim=1).view(-1, 1)
        if len(others) == 0:
            return result
        else:
            return (result, *others)


    def apply_transform(self,s):
        transform = transforms.ToTensor()
        return transform(s).unsqueeze(0)
    def act_discrete_with_noise(
        self,
        state: Dict[str, Any],
        use_target: bool = False,
        decay_epsilon: bool = True,
        **__,
    ):
        """
        Randomly selects an action from the action space according
        to a uniform distribution, with regard to the epsilon decay
        policy.

        Args:
            state: Current state.
            use_target: Whether to use the target network.
            decay_epsilon: Whether to decay the ``epsilon`` attribute.

        Returns:
            Noisy action of shape ``[batch_size, 1]``.
            Any other things returned by your Q network. if they exist.
        """
        if use_target:
            result, *others = safe_call(self.qnet_target, state)
        else:
            result, *others = safe_call(self.qnet, state)

        action_dim = result.shape[1]
        result = t.argmax(result, dim=1).view(-1, 1)

        if t.rand([1]).item() < self.epsilon:
            result = t.randint(0, action_dim, [result.shape[0], 1])

        if decay_epsilon:
            self.epsilon *= self.epsilon_decay

        if len(others) == 0:
            return result
        else:
            return (result, *others)

    def _act_discrete(self, state: Dict[str, Any], use_target: bool = False, **__):
        """
        Use Q network to produce a discrete action for
        the current state.

        Args:
            state: Current state.
            use_target: Whether to use the target network.

        Returns:
            Action of shape ``[batch_size, 1]``
        """
        if use_target:
            result, *others = safe_call(self.qnet_target, state)
        else:
            result, *others = safe_call(self.qnet, state)
        return t.argmax(result, dim=1).view(-1, 1)

    def _criticize(self, state: Dict[str, Any], use_target: bool = False, **__):
        """
        Use Q network to evaluate current value.

        Args:
            state: Current state.
            use_target: Whether to use the target network.
        """
        if use_target:
            return safe_call(self.qnet_target, state)[0]
        else:
            return safe_call(self.qnet, state)[0]

    def store_episode(self, episode: List[Union[Transition, Dict]]):
        """
        Add a full episode of transition samples to the replay buffer.
        """
        self.replay_buffer.store_episode(
            episode,
            required_attrs=("state", "action", "reward", "next_state", "terminal"),
        )

    def update(
        self, update_value=True, update_target=True, concatenate_samples=True, **__
    ):
        """
        Update network weights by sampling from replay buffer.

        Args:
            update_value: Whether update the Q network.
            update_target: Whether update targets.
            concatenate_samples: Whether concatenate the samples.

        Returns:
            mean value of estimated policy value, value loss
        """
        device = t.device('cuda' if t.cuda.is_available() else 'cpu')
        
        batch, weights, indices = self.replay_buffer.sample()

        # print("reward",terminal)
        #terminal  = terminal["terminal"] 
        #reward = reward["reward"]
        #print("reward",reward)
        #print("action",action.keys,type(action["action"]))
        #print("reward",reward)
        #self.qnet.train()
        if self.mode == "vanilla":
            # Vanilla DQN, directly optimize q network.
            # target network is the same as the main network
            q_value = self._criticize(state)
            # gather requires long tensor, int32 is not accepted
            action_value = q_value.gather(
                dim=1,
                index=self.action_get_function(action).to(
                    device=q_value.device, dtype=t.long
                ),
            )

            target_next_q_value = (
                t.max(self._criticize(next_state), dim=1)[0].unsqueeze(1).detach()
            )
            y_i = self.reward_function(
                reward, self.discount, target_next_q_value, terminal, others
            )
            value_loss = self.criterion(action_value, y_i.type_as(action_value))

            if self.visualize:
                self.visualize_model(value_loss, "qnet", self.visualize_dir)

            if update_value:
                self.qnet.zero_grad()
                value_loss.backward()
                nn.utils.clip_grad_norm_(self.qnet.parameters(), self.grad_max)
                self.qnet_optim.step()

        elif self.mode == "fixed_target":
            # Fixed target DQN, which estimate next value by using the
            # target Q network. Similar to the idea of DDPG.
            q_value = self._criticize(state)

            # gather requires long tensor, int32 is not accepted
            action_value = q_value.gather(
                dim=1,
                index=self.action_get_function(action).to(
                    device=q_value.device, dtype=t.long
                ),
            )

            target_next_q_value = (
                t.max(self._criticize(next_state, True), dim=1)[0].unsqueeze(1).detach()
            )

            y_i = self.reward_function(
                reward, self.discount, target_next_q_value, terminal, others
            )
            value_loss = self.criterion(action_value, y_i.type_as(action_value))
            
          

            if self.visualize:
                self.visualize_model(value_loss, "qnet", self.visualize_dir)

            if update_value:
                self.qnet.zero_grad()
                self._backward(value_loss)
                nn.utils.clip_grad_norm_(self.qnet.parameters(), self.grad_max)
                self.qnet_optim.step()

            
            # Update target Q network
            if update_target:
                soft_update(self.qnet_target, self.qnet, self.update_rate)

        elif self.mode == "double":
            # Double DQN. DDQN also use the target network to estimate the next
            # value, but instead of selecting the maximum Q(s,a), it uses
            # the online DQN network to select an action and return Q(s,a'), to
            # reduce the over estimation.
            #print(next_state.keys())
            #print("state length:",len(batch.state), " action length:",len(batch.action)," reward length:", len(batch.reward), " next state length: ",len(batch.next_state))
            
            device = t.device('cuda' if t.cuda.is_available() else 'cpu')
            state_batch = t.cat([self.apply_transform(s) for s in batch.state]).to(device)  # (32, 4, 96, 96)
            action_batch = t.tensor(batch.action, dtype=t.long).to(device)  # (32,)
            reward_batch = t.tensor(batch.reward, dtype=t.float32).to(device)  # (32,)
            non_final_next_states = t.cat([self.apply_transform(s) for s in batch.next_state if s is not None]).to(device, non_blocking=True)  # (<=32, 4, 96, 96)
            #print(state_batch.size(),action_batch.size(),reward_batch.size(),non_final_next_states.size())
            output = self.qnet(state_batch) 
            #next_state2 = {"state": non_final_next_states}
            #print(type(action),type(state))
            #q_value = self._criticize(state) # (32, 4, 96, 96)

            # gather requires long tensor, int32 is not accepted
            
            #action_value = q_value.gather(
            #    dim=1,
            #    index=self.action_get_function(action).to(
            #        device=q_value.device, dtype=t.long
            #    ),
            #) 
            #action_batch = t.tensor(action, dtype=t.long).to(q_value.device) 
            #reward_batch = t.tensor(batch.reward, dtype=t.float32).to(device)  # (32,)

            #non_final_next_states = t.cat([self.apply_transform(s) for s in next_state if s is not None]).to(device, non_blocking=True)  # (<=32, 4, 96, 96)
            state_action_values = output.view(self.batch_size, -1).gather(1, action_batch.unsqueeze(1)).squeeze(1)  # (32,)

            next_state_values = t.zeros(self.batch_size, dtype=t.float32, device=device)  # (32,)
            non_final_mask = t.tensor(tuple(map(lambda s: s is not None, batch.next_state)), dtype=t.bool, device=device)  # (32,)


            with t.no_grad():
            #target_next_q_value = self._criticize(, True)
                best_action = self.qnet(non_final_next_states).view(non_final_next_states.size(0), -1).max(1)[1].view(non_final_next_states.size(0), 1)  # (<=32, 1)
                next_state_values[non_final_mask] = self.qnet_target(non_final_next_states).view(non_final_next_states.size(0), -1).gather(1, best_action).view(-1)  # (<=32,)
                
            

            #y_i = self.reward_function(
            #    reward, self.discount, target_next_q_value, terminal, others
            #)
            expected_state_action_values = (reward_batch + self.discount * next_state_values)  # (32
            td_error = t.abs(state_action_values - expected_state_action_values)  # (32,)
            value_loss = smooth_l1_loss(state_action_values, expected_state_action_values)#y_i.type_as(action_value))

            value_loss = value_loss * t.from_numpy(np.array(weights)).view([self.batch_size, 1]).to(value_loss.device)
            value_loss = value_loss.mean()
            td_error = td_error.detach().cpu().numpy()
            #delta = t.sum(td_error,dim=0).flatten().detach().cpu().numpy()

            if self.visualize:
                self.visualize_model(value_loss, "qnet", self.visualize_dir)

            if update_value:
                self.qnet_optim.zero_grad()
                value_loss.backward()
                #self._backward(value_loss)
                nn.utils.clip_grad_norm_(self.qnet.parameters(), self.grad_max)
                self.qnet_optim.step()

            self.replay_buffer.update_priorities(td_error, indices) 

            # Update target Q network
            if update_target:
                if self.update_rate is not None:
                    soft_update(self.qnet_target, self.qnet, self.update_rate)
                else:
                    self._update_counter += 1
                    if self._update_counter % self.update_steps == 0:
                        hard_update(self.qnet_target, self.qnet)

        else:
            raise ValueError(f"Unknown DQN mode: {self.mode}")

        #self.qnet.eval()
        # use .item() to prevent memory leakage
        #return value_loss.item()

    def update_lr_scheduler(self):
        """
        Update learning rate schedulers.
        """
        if hasattr(self, "qnet_lr_sch"):
            self.qnet_lr_sch.step()

    def load(self, model_dir, network_map=None, version=-1):
        # DOC INHERITED
        super().load(model_dir, network_map, version)
        with t.no_grad():
            hard_update(self.qnet, self.qnet_target)

    @staticmethod
    def action_get_function(sampled_actions):
        """
        This function is used to get action numbers (int tensor indicating
        which discrete actions are used) from the sampled action dictionary.
        """
        return sampled_actions["action"]

    @staticmethod
    def reward_function(reward, discount, next_value, terminal, _):
        next_value = next_value.to(reward.device)
        terminal = terminal.to(reward.device)
        return reward + discount * ~terminal * next_value

    @classmethod
    def generate_config(cls, config: Union[Dict[str, Any], Config]):
        default_values = {
            "models": ["QNet", "QNet"],
            "model_args": ((), ()),
            "model_kwargs": ({}, {}),
            "optimizer": "Adam",
            "criterion": "MSELoss",
            "criterion_args": (),
            "criterion_kwargs": {},
            "lr_scheduler": None,
            "lr_scheduler_args": None,
            "lr_scheduler_kwargs": None,
            "batch_size": 100,
            "epsilon_decay": 0.9999,
            "update_rate": 0.005,
            "update_steps": None,
            "learning_rate": 0.001,
            "discount": 0.99,
            "gradient_max": np.inf,
            "replay_size": 500000,
            "replay_device": "cpu",
            "replay_buffer": None,
            "mode": "double",
            "visualize": False,
            "visualize_dir": "",
        }
        config = deepcopy(config)
        config["frame"] = "DQN"
        if "frame_config" not in config:
            config["frame_config"] = default_values
        else:
            config["frame_config"] = {**config["frame_config"], **default_values}
        return config

    @classmethod
    def init_from_config(
        cls,
        config: Union[Dict[str, Any], Config],
        model_device: Union[str, t.device] = "cpu",
    ):
        f_config = deepcopy(config["frame_config"])
        models = assert_and_get_valid_models(f_config["models"])
        model_args = f_config["model_args"]
        model_kwargs = f_config["model_kwargs"]
        models = [
            m(*arg, **kwarg).to(model_device)
            for m, arg, kwarg in zip(models, model_args, model_kwargs)
        ]
        optimizer = assert_and_get_valid_optimizer(f_config["optimizer"])
        criterion = assert_and_get_valid_criterion(f_config["criterion"])(
            *f_config["criterion_args"], **f_config["criterion_kwargs"]
        )
        lr_scheduler = f_config["lr_scheduler"] and assert_and_get_valid_lr_scheduler(
            f_config["lr_scheduler"]
        )
        f_config["optimizer"] = optimizer
        f_config["criterion"] = criterion
        f_config["lr_scheduler"] = lr_scheduler
        frame = cls(*models, **f_config)
        return frame
