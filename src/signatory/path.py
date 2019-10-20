# Copyright 2019 Patrick Kidger. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# =========================================================================
"""Provides the Path class, a high-level object capable of giving signatures over intervals."""

import bisect
import torch
from torch import autograd
from torch.autograd import function as autograd_function

from . import backend
from . import signature_module as smodule
from . import logsignature_module as lmodule
from . import _impl

# noinspection PyUnreachableCode
if False:
    from typing import List, Union


class _BackwardShortcut(autograd.Function):
    @staticmethod
    def forward(ctx, signature, depth, stream, basepoint, inverse, initial, *path_pieces):
        if len(path_pieces) == 0:
            raise ValueError('path_pieces must have nonzero length')

        # Record the tensors upon which the backward calculation depends
        save_for_backward = list(path_pieces)
        ctx.path_pieces_end = len(save_for_backward)
        ctx.signature_index = len(save_for_backward)
        save_for_backward.append(signature)
        if isinstance(basepoint, torch.Tensor):
            ctx.basepoint_is_tensor = True
            ctx.basepoint_index = len(save_for_backward)
            save_for_backward.append(basepoint)
        else:
            ctx.basepoint_is_tensor = False
            ctx.basepoint = basepoint
        if isinstance(initial, torch.Tensor):
            ctx.initial_is_tensor = True
            ctx.initial_index = len(save_for_backward)
            save_for_backward.append(initial)
        else:
            ctx.initial_is_tensor = False
            ctx.initial = initial

        ctx.depth = depth
        ctx.stream = stream
        ctx.inverse = inverse

        ctx.save_for_backward(*save_for_backward)

        return signature

    @staticmethod
    @autograd_function.once_differentiable  # Our backward function uses in-place operations for memory efficiency
    def backward(ctx, grad_result):
        # Test for any in-place changes
        # This isn't perfect. If any of the stored tensors do not own their own storage (which is possible) then
        # this check will always pass, even if they've been modified in-place. (PyTorch bug 24413)
        saved_tensors = ctx.saved_tensors
        path_pieces = saved_tensors[:ctx.path_pieces_end]
        signature = saved_tensors[ctx.signature_index]
        if ctx.basepoint_is_tensor:
            basepoint = saved_tensors[ctx.basepoint_index]
        else:
            basepoint = ctx.basepoint
        if ctx.initial_is_tensor:
            initial = saved_tensors[ctx.initial_index]
        else:
            initial = ctx.initial

        if len(path_pieces) > 1:
            # TODO: This concatenation isn't really necessary. Pretty much the only thing we do with this is to compute
            #       the increments. Not sure what the most elegant way to adjust _impl.signature_backward_custom to take
            #       advantage of that is, though.
            path = torch.cat(path_pieces, dim=-3)  # along stream dim
        else:
            path = path_pieces[0]

        basepoint, basepoint_value, initial, initial_value = smodule.interpet_forward_args(ctx, path, basepoint,
                                                                                           initial)

        grad_path, grad_basepoint, grad_initial = _impl.signature_backward_custom(grad_result,
                                                                                  signature,
                                                                                  path,
                                                                                  ctx.depth,
                                                                                  ctx.stream,
                                                                                  basepoint,
                                                                                  basepoint_value,
                                                                                  ctx.inverse,
                                                                                  initial)

        grad_basepoint, grad_initial = smodule.interpret_backward_grad(ctx, grad_basepoint, grad_initial)

        result = [None, None, None, grad_basepoint, None, grad_initial]
        start = 0
        end = 0
        for elem in path_pieces:
            end += elem.size(-3)  # stream dimension
            result.append(grad_path[:, start:end, :])
            start = end
        return tuple(result)


# This wires up a shortcut through the backward operation.
# The already-computed signature is just returned during the forward operation.
# And the backward operation through signature is not computed in favour of shortcutting through path_pieces. (Which
# is assumed to be the path which has this signature!)
def _backward_shortcut(signature, path_pieces, depth, stream, basepoint, inverse, initial):
    # (batch, stream, channel) to (stream, batch, channel)
    path_pieces = [path_piece.transpose(0, 1) for path_piece in path_pieces]
    # .detach() so that no gradients are taken through this argument
    result = _BackwardShortcut.apply(signature.detach(), depth, stream, basepoint, inverse, initial, *path_pieces)

    if stream:
        return result.transpose(0, 1)
    else:
        return result


class Path(object):
    """Calculates signatures on intervals of an input path.

    By doing some precomputation, it can rapidly calculate the signature of the input path over any interval. This is
    particularly useful if you need the signature of a Path over many different intervals: using this class will be much
    faster than computing the signature of each sub-path each time.

    Arguments:
        path (torch.Tensor): As :func:`signatory.signature`.

        depth (int): As :func:`signatory.signature`.

        basepoint (bool or torch.Tensor, optional): As :func:`signatory.signature`.
    """
    def __init__(self, path, depth, basepoint=False):
        # type: (torch.Tensor, int, Union[bool, torch.Tensor]) -> None
        self._depth = depth

        self._signature = []
        self._inverse_signature = []

        self._path = []

        self._length = 0
        self._signature_length = 0
        self._lengths = []
        self._signature_lengths = []

        self._batch_size = path.size(-3)
        self._channels = path.size(-1)
        self._signature_channels = smodule.signature_channels(self._channels, self._depth)
        self._logsignature_channels = lmodule.logsignature_channels(self._channels, self._depth)

        use_basepoint, basepoint_value = backend.interpret_basepoint(basepoint, path.size(0), path.size(2), path.dtype,
                                                                     path.device)
        if use_basepoint:
            self._length += 1
            self._lengths.append(1)
            self._path.append(basepoint_value.unsqueeze(-2))  # unsqueeze a stream dimension

        self._update(path, basepoint, None, None)

        self._signature_to_logsignature_instances = {}

    def signature(self, start=None, end=None):
        # type: (Union[int, None], Union[int, None]) -> torch.Tensor
        """Returns the signature on a particular interval.

        Arguments:
            start (int or None, optional): Defaults to the start of the path. The start point of the interval to 
                calculate the signature on.

            end (int or None, optional): Defaults to the end of the path. The end point of the interval to calculate
                the signature on.

        Returns:
            The signature on the interval :attr:`[start, end]`.

            Let :attr:`p = torch.cat(self.path, dim=1)`, so that it is all given paths (from both initialisation and
            :meth:`signatory.Path.update`) concatenated together, additionally with any basepoint prepended. Then this
            function will return a value equal to :attr:`signatory.signature(p[start:end], depth)`.
        """

        # Record for error messages if need be
        old_start = start
        old_end = end

        # Interpret start and end in the same way as slicing behaviour
        if start is None:
            start = 0
        if end is None:
            end = self._length
        if start < -self._length:
            start = -self._length
        elif start > self._length:
            start = self._length
        if end < -self._length:
            end = -self._length
        elif end > self._length:
            end = self._length
        if start < 0:
            start += self._length
        if end < 0:
            end += self._length

        # Check that start and end are valid
        if end - start == 1:
            # Friendlier help message for a common mess-up.
            raise ValueError("start={}, end={} is interpreted as {}, {} for path of length {}, which "
                             "does not describe a valid interval. The given start and end differ by only one, but "
                             "a single point is not sufficent to define a path."
                             .format(old_start, old_end, start, end, self._length))
        if end - start < 2:
            raise ValueError("start={}, end={} is interpreted as {}, {} for path of length {}, which "
                             "does not describe a valid interval.".format(old_start, old_end, start, end, self._length))

        # Find the signature on [:end]
        sig_end = end - 2
        index_sig_end, sig_end = self._locate(self._signature_lengths, sig_end)
        sig_at_end = self._signature[index_sig_end][:, sig_end, :]

        # If start takes its minimum value then that's all we need to return
        if start == 0:
            return sig_at_end

        # Find the inverse signature on [:start]
        sig_start = start - 1
        index_sig_start, sig_start = self._locate(self._signature_lengths, sig_start)
        inverse_sig_at_start = self._inverse_signature[index_sig_start][:, sig_start, :]

        # Find the signature on [start:end]
        signature = smodule.multi_signature_combine([inverse_sig_at_start, sig_at_end], self._channels, self.depth)

        # Find path[start:end]
        path_pieces = []
        index_end, end = self._locate(self._lengths, end)
        index_start, start = self._locate(self._lengths, start)
        path_pieces.append(self.path[index_start][:, start:, :])
        for path_piece in self.path[index_start + 1:index_end]:
            path_pieces.append(path_piece)
        if end != 0:
            # self.path[index_end] is off-the-end if end == 0
            # and the path we'd append here is of zero length
            path_pieces.append(self.path[index_end][:, :end, :])

        # We know that we're only returning the signature on [start:end], and that there is no dependence on the region
        # [0:start]. But if we were to compute the backwards operation naively then this information wouldn't be used.
        #
        # What's returned would be treated as inverse_sig[0:start] \otimes sig[0:end] and we'd backprop through the
        # whole [0:start] region unnecessarily. We'd end up doing a whole lot of work to find that there's a zero
        # gradient on path[0:start].
        # (Or actually probably find that there's some very small gradient due to floating point errors...)
        #
        # This obviously isn't desirable if start takes a large value - lots of unnecessary work - so here we insert a
        # custom backwards that shortcuts that whole procedure.
        return _backward_shortcut(signature, path_pieces, self._depth, False, False, False, False)

    @staticmethod
    def _locate(lengths, index):
        lengths_index = bisect.bisect_right(lengths, index)
        if lengths_index > 0:
            index -= lengths[lengths_index - 1]
        return lengths_index, index

    def logsignature(self, start=None, end=None, mode="words"):
        # type: (Union[int, None], Union[int, None], str) -> torch.Tensor
        """Returns the logsignature on a particular interval.

        Arguments:
            start (int or None, optional): As :meth:`signatory.Path.signature`.

            end (int or None, optional): As :meth:`signatory.Path.signature`.

            mode (str, optional): As :func:`signatory.logsignature`.

        Returns:
            The logsignature on the interval :attr:`[start, end]`. See the documentation for
            :meth:`signatory.Path.signature`.
        """
        signature = self.signature(start, end)
        try:
            signature_to_logsignature_instance = self._signature_to_logsignature_instances[(self._channels,
                                                                                            self._depth,
                                                                                            mode)]
        except KeyError:
            signature_to_logsignature_instance = lmodule.SignatureToLogSignature(self._channels, self._depth,
                                                                                 stream=False, mode=mode)
            self._signature_to_logsignature_instances[(self._channels, self._depth, mode)] = signature_to_logsignature_instance
        return signature_to_logsignature_instance(signature)

    def update(self, path):
        # type: (torch.Tensor) -> None
        """Concatenates the given path onto the path already stored.

        This means that the signature of the new overall path can now be asked for via :meth:`signatory.Path.signature`.
        Furthermore this will be dramatically faster than concatenating the two paths together and then creating a new
        Path object: the 'concatenation' occurs implicitly, without actually involving any recomputation or reallocation
        of memory.

        Arguments:
            path (torch.Tensor): The path to concatenate on. As :func:`signatory.signature`.
        """
        if path.size(-3) != self._batch_size:
            raise ValueError("Cannot append a path with different number of batch elements to what has already been "
                             "used.")
        if path.size(-1) != self._channels:
            raise ValueError("Cannot append a path with different number of channels to what has already been used.")
        basepoint = self._path[-1][:, -1, :]
        initial = self._signature[-1][:, -1, :]
        inverse_initial = self._inverse_signature[-1][:, -1, :]
        self._update(path, basepoint, initial, inverse_initial)

    def _update(self, path, basepoint, initial, inverse_initial):
        signature = smodule.signature(path, self._depth, stream=True, basepoint=basepoint, initial=initial)
        inverse_signature = smodule.signature(path, self._depth, stream=True, basepoint=basepoint, inverse=True,
                                              initial=inverse_initial)
        self._signature.append(signature)
        self._inverse_signature.append(inverse_signature)

        self._path.append(path)

        self._length += path.size(-2)
        self._signature_length += signature.size(-2)
        self._lengths.append(self._length)
        self._signature_lengths.append(self._signature_length)

    @property
    def path(self):
        # type: () -> List[torch.Tensor]
        """The path(s) that this Path was created with."""
        return self._path

    @property
    def depth(self):
        # type: () -> int
        """The depth that Path has calculated the signature to."""
        return self._depth

    def size(self, index=None):
        # type: (Union[int, None]) -> Union[int, torch.Size]
        """The size of the input path. As :meth:`torch.Tensor.size`.

        Arguments:
            index (int or None, optional): As :meth:`torch.Tensor.size`.

        Returns:
            As :meth:`torch.Tensor.size`.
        """
        if index is None:
            return self.shape
        else:
            return self.shape[index]

    @property
    def shape(self):
        # type: () -> torch.Size
        """The shape of the input path. As :attr:`torch.Tensor.shape`."""
        return torch.Size([self._batch_size, self._length, self._channels])

    # Method not property for consistency with signature_channels and logsignature_channels
    def channels(self):
        # type: () -> int
        """The number of channels of the input stream."""
        return self._channels

    def signature_size(self, index=None):
        # type: (Union[int, None]) -> Union[int, torch.Size]
        """The size of the signature of the path. As :meth:`torch.Tensor.size`.

        Arguments:
            index (int or None, optional): As :meth:`torch.Tensor.size`.

        Returns:
            As :meth:`torch.Tensor.size`.
        """
        if index is None:
            return self.signature_shape
        else:
            return self.signature_shape[index]

    @property
    def signature_shape(self):
        # type: () -> torch.Size
        """The shape of the signature of the path. As :attr:`torch.Tensor.shape`."""
        return torch.Size([self._batch_size, self._signature_length, self._signature_channels])

    # Method not property for consistency with signatory.signature_channels
    def signature_channels(self):
        # type: () -> int
        """The number of signature channels; as :func:`signatory.signature_channels`."""
        return self._signature_channels

    def logsignature_size(self, index=None):
        # type: (Union[int, None]) -> Union[int, torch.Size]
        """The size of the logsignature of the path. As :meth:`torch.Tensor.size`.

        Arguments:
            index (int or None, optional): As :meth:`torch.Tensor.size`.

        Returns:
            As :meth:`torch.Tensor.size`.
        """
        if index is None:
            return self.logsignature_shape
        else:
            return self.logsignature_shape[index]

    @property
    def logsignature_shape(self):
        # type: () -> torch.Size
        """The shape of the logsignature of the path. As :attr:`torch.Tensor.shape`."""
        return torch.Size([self._batch_size, self._signature_length, self._logsignature_channels])

    # Method not property for consistency with signatory.signature_channels
    def logsignature_channels(self):
        # type: () -> int
        """The number of logsignature channels; as :func:`signatory.logsignature_channels`."""
        return self._logsignature_channels
