/* Copyright 2019 Patrick Kidger. All Rights Reserved.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 * 
 *    http://www.apache.org/licenses/LICENSE-2.0
 * 
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 * ========================================================================= */


#include <torch/extension.h>
#include <cstdint>      // int64_t
#include <vector>       // std::vector


namespace signatory {
    namespace misc {
        torch::TensorOptions make_opts(torch::Tensor tensor) {
            return torch::TensorOptions().dtype(tensor.dtype()).device(tensor.device());
        }

        torch::Tensor make_reciprocals(s_size_type depth, torch::TensorOptions opts) {
            if (depth > 1) {
                return torch::ones({depth - 1}, opts) /
                       torch::linspace(2, static_cast<torch::Scalar>(static_cast<int64_t>(depth)), depth - 1, opts);
                                          // Cast to torch::Scalar is ambiguous
            }
            else {
                return torch::ones({0}, opts);
            }
        }

        inline void slice_by_term(torch::Tensor in, std::vector<torch::Tensor>& out, int64_t input_channel_size,
                                  s_size_type depth) {
            int64_t current_memory_pos = 0;
            int64_t current_memory_length = input_channel_size;
            out.clear();
            out.reserve(depth);
            for (int64_t i = 0; i < depth; ++i) {
                out.push_back(in.narrow(/*dim=*/channel_dim,
                                        /*start=*/current_memory_pos,
                                        /*len=*/current_memory_length));
                current_memory_pos += current_memory_length;
                current_memory_length *= input_channel_size;
            }
        }

        inline void slice_at_stream(const std::vector<torch::Tensor>& in, std::vector<torch::Tensor>& out,
                                    int64_t stream_index) {
            out.clear();
            out.reserve(in.size());
            for (auto elem : in) {
                out.push_back(elem[stream_index]);
            }
        }
    }  // namespace signatory::misc
}  // namespace signatory