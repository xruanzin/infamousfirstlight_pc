// SPDX-License-Identifier: GPL-2.0-or-later
#pragma once

#include <atomic>

namespace AmdGpu {

// First Light/AMD compatibility: some proprietary Windows drivers reject or crash on
// small block-compressed 1D images.  The image cache can transparently decode BC1-BC5
// into ordinary 1D images before Vulkan sees them.
inline std::atomic<bool> bc_tiny_decode{false};
// First Light BP09-style compatibility: host every guest 1D image as a 2D image of height 1.
// This is set before image/shader creation so descriptors, cache entries and SPIR-V agree.
inline std::atomic<bool> all_1d_as_2d{false};

inline bool All1DAs2D() {
    return all_1d_as_2d.load(std::memory_order_relaxed);
}

inline bool BcTinyDecode() {
    return bc_tiny_decode.load(std::memory_order_relaxed);
}

} // namespace AmdGpu
