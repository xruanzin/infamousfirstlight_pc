// SPDX-License-Identifier: GPL-2.0-or-later
#pragma once

#include <atomic>

namespace AmdGpu {

// First Light/AMD compatibility: some proprietary Windows drivers reject or crash on
// small block-compressed 1D images.  The image cache can transparently decode BC1-BC5
// into ordinary 1D images before Vulkan sees them.
inline std::atomic<bool> bc_tiny_decode{false};

inline bool BcTinyDecode() {
    return bc_tiny_decode.load(std::memory_order_relaxed);
}

} // namespace AmdGpu
