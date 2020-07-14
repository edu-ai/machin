from typing import Any, Union
from random import choice
from copy import deepcopy
from queue import Queue
from threading import Event

import torch as t
import torch.nn as nn

from machin.parallel.thread import Thread
from machin.parallel.distributed import RpcGroup
from machin.utils.prepare import prep_load_state_dict
from .ordered_server import OrderedServerBase, OrderedServerSimple


class PushPullModelServer:
    """
    A simple parameter server, which synchronize model parameters
    by pushing and pulling all parameters and maintaining a strict
    ordered version chain.

    Warning:
        ``DistributedDataParallel`` is not supported.
    """

    def __init__(self,
                 model_name: str,
                 group: RpcGroup,
                 server: OrderedServerBase = None):
        """
        Args:
            model_name: Name of the model.
            group: RpcGroup of the default server :class:`.OrderedServerSimple`
                mutually exclusive with ``server``
            server: Custom ordered server.
        """
        self.model_name = model_name
        self.server = (
            OrderedServerSimple(
                model_name + "_pp_server",
                group.get_group_members()[0],
                group
            )
            if server is None
            else server
        )

    def push(self, model: nn.Module):
        """
        Try to push a model to the ordered server, if failed, the newest
        model will be automatically pulled and its parameters will be
        assigned to ``model``. Gradients will not be cleared.

        Args:
            model: Model to push.
        """
        if not hasattr(model, "pp_version"):
            model.pp_version = 0

        copied_model = deepcopy(model)
        if not self.server.push(
                self.model_name, copied_model.to("cpu").state_dict(),
                version=model.pp_version + 1, prev_version=model.pp_version
        ):
            result = self.server.pull(self.model_name)
            if result is None:  # pragma: no cover
                raise RuntimeError("Pull failed, this should not happen.")
            st_dict, version = result
            prep_load_state_dict(model, st_dict)
            model.pp_version = version
            return False
        else:
            model.pp_version += 1
        return True

    def pull(self, model: nn.Module):
        """
        Pull the newest state dict of your model and update its parameters
        and ``pp_version``. Gradients will not be cleared.

        Args:
            model: Model to pull.
        """
        result = self.server.pull(self.model_name)
        if result is None:  # pragma: no cover
            return False
        st_dict, version = result
        if not hasattr(model, "pp_version") or model.pp_version < version:
            prep_load_state_dict(model, st_dict)
            model.pp_version = version
        return True


class PushPullGradServer:
    """
    A simple parameter server, which synchronize model parameters
    by pushing gradients and pulling back new parameters, no strict
    order is guaranteed.

    Warning:
        ``DistributedDataParallel`` is not supported. since we cannot
        load state dictionary after creation.
    """
    REDUCE_MASTER = 0
    REDUCE_SLAVE = 1

    def __init__(self,
                 model_name: str,
                 group: RpcGroup,
                 master_reducer: str = None,
                 server: OrderedServerBase = None,
                 reduce_device: Union[t.device, str] = "cpu",
                 reduce_batch_size: int = 64,
                 max_queue_size: int = 1024):
        """
        Note:
            You should initialize ``PushPullGradServer`` on all roles of the
            rpc ``group``, and ``master_reduce_role`` must be one member of
            the rpc group.

        Note:
            Internally the master reducer will push updated versions
            to the ordered server.

        Hint:
            Reduction is performed in a tree fashion:

            1. In the first step, clients will push new gradients to a
               random slave reducer, and the slave reducer will perform
               the first reduction pass, then slave reducers will push
               their results to the master reducer.
            2. In the second step, the master reducer will reduce results
               from the slave reducer to get the final reduced gradient
               dictionary (has the same structure as state_dict), and assign
               gradients to its **managed model**, and perform the
               optimization.
            3. In the final step, the master reducer will push the final
               model to the model server group, then clients can pull the
               newest model.

        Args:
            model_name: Name of the model.
            group: Server group.
            master_reducer: Name of the process serving as the master reducer,
                which collects reduced gradients from slave reducers and
                perform the final reduction.
            server: Custom ordered server, mutually exclusive with
                ``master_reducer``.
            reduce_device: Device to perform reduction, by default it is "cpu".
            reduce_batch_size: Size of a single reduction batch, server will
                wait until the number of requests in the reduction queue have
                reached this size.
            max_queue_size: Maximum reduction request queue size.
        """
        self.model_name = model_name
        self.server = (
            OrderedServerSimple(
                model_name + "_pp_server",
                group.get_group_members()[0],
                group
            )
            if server is None
            else server
        )
        self.server_name = model_name + "_pp_grad_server"
        self.group = group
        self.group.rpc_pair(model_name + "_pp_grad_server", self)
        if master_reducer is None:
            master_reducer = group.get_group_members()[0]
        assert group.is_member(master_reducer)
        self.reduce_master = master_reducer
        self.reduce_slaves = self.group.get_group_members()
        self.reduce_batch_size = reduce_batch_size
        self.reduce_device = reduce_device
        self.model = None  # type: Union[nn.Module, None]
        self.optimizer = None

        # prepare to start the reduction subprocess
        self.master_queue = Queue(maxsize=max_queue_size)
        self.slave_queue = Queue(maxsize=max_queue_size)
        self.work_event = Event()
        self.stop_event = Event()
        self.reduce_proc = Thread(target=self._task_reduce_grad)

    def start(self):
        self.reduce_proc.start()

    def stop(self):
        self.stop_event.set()
        self.reduce_proc.join()
        self.stop_event.clear()

    def watch(self):
        self.reduce_proc.watch()

    def push(self, model: nn.Module):
        """
        Push the gradients of your model, then pull the newest parameters.
         Its gradients will be cleared.

        Args:
            model: Model to push.
        """
        # extract gradients from the model
        grad_dict = {}
        for k, v in model.named_parameters():
            grad_dict[k] = deepcopy(v.grad).to("cpu")
        self.group.rpc_paired_class_async(
            choice(self.reduce_slaves),
            self.server_name,
            self._push_reply,
            args=(grad_dict, self.REDUCE_SLAVE)
        )
        self.pull(model)

    def pull(self, model: nn.Module):
        """
        Pull the newest model. Its gradients will be cleared.

        Args:
            model: Model to push.
        """
        model.zero_grad()
        params = self.server.pull(self.model_name)
        if params is not None:
            # params could be None if the master reducer has't performed
            # a single reduction operation yet
            prep_load_state_dict(model, params[0])

    def manage_model(self, model: nn.Module, optimizer: Any):
        """
        Let the main reducer manage your model. Must be called before start.

        Args:
            model: Model to manage.
            optimizer: Optimizer of your model. you should initialize it first:
            >>> optimizer(model.parameters(), lr=1e-3)

        Raises:
            ``RuntimeError`` if current rpc role is not the main reducer.
        """
        if self.group.get_cur_name() == self.reduce_master:
            self.model = model
            self.optimizer = optimizer
        else:  # pragma: no cover
            raise RuntimeError("Current worker is not the reduce master, and"
                               "cannot manage the model.")

    def _push_reply(self, grad_dict, level):  # pragma: no cover
        # Append reduce requests to queue.
        if level == self.REDUCE_SLAVE:
            self.slave_queue.put_nowait(grad_dict)
            self.work_event.set()
            self.work_event.clear()
        elif level == self.REDUCE_MASTER:
            self.master_queue.put_nowait(grad_dict)
            self.work_event.set()
            self.work_event.clear()

    def _task_reduce_grad(self):
        while True:
            # Wait until one queue has reached target batch size
            while (self.master_queue.qsize() < self.reduce_batch_size and
                   self.slave_queue.qsize() < self.reduce_batch_size):
                self.work_event.wait(timeout=1e-1)
                if self.stop_event.is_set():
                    return
            if self.master_queue.qsize() >= self.reduce_batch_size:
                # Perform reduction on the master reduction queue
                # Only the master reducer will execute this branch
                grad_dict = self._reduce_batch(self.master_queue,
                                               self.reduce_batch_size,
                                               self.reduce_device)
                # Assign gradients to the managed model and
                # perform optimization.
                if self.model is not None and self.optimizer is not None:
                    self.optimizer.zero_grad()
                    with t.no_grad():
                        for k, v in self.model.named_parameters():
                            v.grad = grad_dict[k].to(v.device)
                    self.optimizer.step()
                    self.server.push(self.model_name,
                                     self.model.to("cpu").state_dict(), 0, 0)

            if self.slave_queue.qsize() >= self.reduce_batch_size:
                # Perform reduction on the slave reduction queue
                # All processes(including master) in the reduction
                # group will execute this branch.
                grad_dict = self._reduce_batch(self.slave_queue,
                                               self.reduce_batch_size,
                                               self.reduce_device)
                # Push reduced results to the master queue.
                self.group.rpc_paired_class_async(
                    self.reduce_master,
                    self.server_name,
                    self._push_reply,
                    args=(grad_dict, self.REDUCE_MASTER)
                )

    @staticmethod
    def _reduce_batch(queue, batch_size, reduce_device):
        """
        Perform batched gradient reduction

        Returns:
            Reduced gradient dictionary.
        """
        batch = []
        while len(batch) < batch_size:
            batch.append(queue.get())
        grad_dict = {}
        for grad in batch:
            for k, v in grad.items():
                if k not in grad_dict:
                    grad_dict[k] = [v.to(reduce_device)]
                else:
                    grad_dict[k].append(v.to(reduce_device))
        for k, v in grad_dict.items():
            # Stack parameter tensors in dim 0 and reduce.
            grad_dict[k] = t.mean(t.cat(v, dim=0), dim=0, keepdim=True)
        return grad_dict
