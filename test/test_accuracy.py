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
"""Tests the accuracy of our operations by comparing against iisignature."""


import signatory
import torch

import compatibility as compat
import utils_testing as utils


class TestSignatureAccuracy(utils.EnhancedTestCase):
    def test_forward(self):
        for c in utils.ConfigIter(inverse=False):
            signatory_out = c.signature()
            iisignature_out = c.sig()
            if not signatory_out.allclose(iisignature_out):
                self.fail(c.fail(signatory_out=signatory_out, iisignature_out=iisignature_out))

    def test_backward(self):
        for c in utils.ConfigIter(stream=False,  # iisignature doesn't support backwards with stream=True
                                  requires_grad=True,
                                  inverse=False):
            c.signature()
            c.sig()
            signatory_grad = c.signature_backward()
            iisignature_grad = c.sig_backward()

            # strangely iisignature returns float32 in the backward calculation, even if the input was float64, so we
            # have to reduce the tolerance quite a lot (https://github.com/bottler/iisignature/issues/7)
            if not signatory_grad.allclose(iisignature_grad, atol=5e-6):
                self.fail(c.diff_fail(signatory_grad=signatory_grad, iisignature_grad=iisignature_grad))

    @staticmethod
    def _reverse_path(path, basepoint):
        basepoint, basepoint_value = signatory.backend.interpret_basepoint(basepoint, path)
        reverse_path = path.flip(1)
        if basepoint:
            reverse_path = torch.cat([reverse_path, basepoint_value.unsqueeze(1)], dim=1)
        return reverse_path

    def test_inverse(self):
        for c in utils.ConfigIter(inverse=True, stream=False):
            inverse_sig = c.signature(store=False)
            reverse_path = self._reverse_path(c.path, c.basepoint)
            true_inverse_sig = c.signature(store=False, path=reverse_path, inverse=False, basepoint=False)
            if not inverse_sig.allclose(true_inverse_sig):
                self.fail(c.diff_fail(inverse_sig=inverse_sig, true_inverse_sig=true_inverse_sig))

    def test_inverse_stream(self):
        for c in utils.ConfigIter(inverse=True, stream=True):
            inverse_sig = c.signature(store=False)
            reverse_path = self._reverse_path(c.path, c.basepoint)
            true_inverse_sig_pieces = []
            for i in range(-2, -reverse_path.size(1) - 1, -1):
                reverse_path_piece = reverse_path[:, i:, :]
                true_inverse_sig_pieces.append(c.signature(store=False, path=reverse_path_piece, inverse=False,
                                                           basepoint=False, stream=False))
            true_inverse_sig = torch.stack(true_inverse_sig_pieces, dim=1)
            if not inverse_sig.allclose(true_inverse_sig):
                self.fail(c.diff_fail(inverse_sig=inverse_sig, true_inverse_sig=true_inverse_sig))


class TestLogSignatureAccuracy(utils.EnhancedTestCase):
    def test_forward(self):
        for c in utils.ConfigIter(mode=(utils.expand, utils.brackets),  # Can't compare mode="words" against iisignature
                                                                        # because it doesn't support that.
                                  C=(2, 3, 6),                          # Can't use C==1 because iisignature.logsig
                                                                        # doesn't support that.
                                  stream=False,                         # Can't use stream=True because
                                                                        # isiignature.logsig doesnt support that.
                                  inverse=False):
            signatory_out = c.logsignature()
            iisignature_out = c.logsig()
            if not signatory_out.allclose(iisignature_out):
                self.fail(c.diff_fail(signatory_out=signatory_out, iisignature_out=iisignature_out))

    def test_forward_words(self):
        for c in utils.ConfigIter(mode=utils.words,
                                  depth=(1, 2, 3),  # We've only coded in the necessary adjustments in the tests between
                                                    # signatory.signature with mode="words" and iisignature.logsig with
                                                    # mode="brackets" for depth<=3
                                  C=(2, 3),         # Can't use C==1 because iisignature.logsig doesn't support that
                                  stream=False,     # Can't use stream=True because isiignature.logsig doesnt support
                                                    # that.
                                  inverse=False):
            signatory_out = c.logsignature()
            iisignature_out = c.logsig()
            if c.C == 3 and c.depth == 3:
                # manually apply the transform from words to brackets
                c.signatory_out[:, 10] += c.signatory_out[:, 9]
            if not signatory_out.allclose(iisignature_out):
                self.fail(c.diff_fail(signatory_out=signatory_out, iisignature_out=iisignature_out))

    def test_forward_stream(self):
        for c in utils.ConfigIter(mode=utils.all_modes,
                                  stream=True,
                                  inverse=False):
            signatory_out = c.logsignature()
            if c.has_basepoint():
                start = 0
            else:
                start = 1
            for subrange in range(start, c.L):
                subpath = c.path[:, :subrange + 1, :]
                subout = signatory.logsignature(subpath, c.depth, stream=False, basepoint=c.basepoint,
                                                mode=c.signatory_mode)
                if c.has_basepoint():
                    offset = 0
                else:
                    offset = 1
                narrowed = signatory_out.narrow(dim=1, start=subrange - offset, length=1).squeeze(1)
                close = narrowed.allclose(subout)
                if not close:
                    self.fail(c.fail(subrange=subrange, subout=subout, narrowed=narrowed, signatory_out=signatory_out))

    # bug in iisignature for this operation (https://github.com/bottler/iisignature/issues/8) so we can't test against
    # them. In any case we have gradchecks in test_gradient.py so this isn't a huge issue.
    @compat.skip
    def test_backward(self):
        for c in utils.ConfigIter(mode=(utils.expand, utils.brackets),
                                  stream=False,  # iisignature doesn't support logsignatures for stream=True
                                  C=(2, 3, 6),   # Can't use C==1 because iisignature.logsig doesn't support that
                                  requires_grad=True,
                                  inverse=False):
            c.logsignature()
            c.logsig()
            signatory_grad = c.logsignature_backward()
            iisignature_grad = c.logsig_backward()
            # strangely iisignature returns float32 in the backward calculation, even if the input was float64, so we
            # have to reduce the tolerance slightly (https://github.com/bottler/iisignature/issues/7)
            if not signatory_grad.allclose(iisignature_grad, atol=5e-6):
                self.fail(c.diff_fail(signatory_grad=signatory_grad, iisignature_grad=iisignature_grad))
