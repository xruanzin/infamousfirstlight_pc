// SPDX-FileCopyrightText: Copyright 2024 shadPS4 Emulator Project
// SPDX-License-Identifier: GPL-2.0-or-later

#include "bbport_toggles.h"
#include <cstdio>
#include <cstdlib>
#include <ranges>
#include "common/assert.h"
#include "video_core/renderer_vulkan/liverpool_to_vk.h"
#include "video_core/renderer_vulkan/vk_instance.h"
#include "video_core/renderer_vulkan/vk_runtime.h"
#include "video_core/renderer_vulkan/vk_scheduler.h"
#include "video_core/texture_cache/blit_helper.h"
#include "video_core/texture_cache/image.h"

#include <vk_mem_alloc.h>

namespace VideoCore {

using namespace Vulkan;

Common::IncrementalIdProvider<u64> Image::global_image_uid{};

static bool FlGpuResourceTrace() {
    static const bool enabled = [] {
        const char* value = std::getenv("FL_GPU_RESOURCE_TRACE");
        return value && value[0] == '1';
    }();
    return enabled;
}

static vk::ImageUsageFlags ImageUsageFlags(const Vulkan::Instance& instance,
                                           const ImageInfo& info) {
    vk::ImageUsageFlags usage = vk::ImageUsageFlagBits::eTransferSrc |
                                vk::ImageUsageFlagBits::eTransferDst |
                                vk::ImageUsageFlagBits::eSampled;
    if (info.props.is_bc_decoded) {
        // CPU-decoded BC fallback images are upload-and-sample resources only.
        return usage;
    }
    if (!info.props.is_block) {
        if (info.props.is_depth) {
            usage |= vk::ImageUsageFlagBits::eDepthStencilAttachment;
        } else {
            usage |= vk::ImageUsageFlagBits::eColorAttachment;
            if (instance.IsAttachmentFeedbackLoopLayoutSupported()) {
                usage |= vk::ImageUsageFlagBits::eAttachmentFeedbackLoopEXT;
            }
            // Always create images with storage flag to avoid needing re-creation in case of e.g
            // compute clears This sacrifices a bit of performance but is less work. ExtendedUsage
            // flag is also used.
            usage |= vk::ImageUsageFlagBits::eStorage;
        }
    } else {
        // Similarly to above, we specify storage usage. This is typically not supported by
        // compressed formats, but may be used for uncompressed views. In order to satisfy this,
        // we will also specify the extended usage bit.
        usage |= vk::ImageUsageFlagBits::eStorage;
    }

    return usage;
}

static vk::ImageType ConvertImageType(AmdGpu::ImageType type) noexcept {
    switch (type) {
    case AmdGpu::ImageType::Color1D:
    case AmdGpu::ImageType::Color1DArray:
        // Preserve the guest dimension. The known-good First Light path reaches Vulkan as 1D.
        return vk::ImageType::e1D;
    case AmdGpu::ImageType::Color2D:
    case AmdGpu::ImageType::Color2DMsaa:
    case AmdGpu::ImageType::Color2DArray:
        return vk::ImageType::e2D;
    case AmdGpu::ImageType::Color3D:
        return vk::ImageType::e3D;
    default:
        UNREACHABLE();
    }
}

static vk::FormatFeatureFlags2 FormatFeatureFlags(const vk::ImageUsageFlags usage_flags) {
    vk::FormatFeatureFlags2 feature_flags{};
    if (usage_flags & vk::ImageUsageFlagBits::eTransferSrc) {
        feature_flags |= vk::FormatFeatureFlagBits2::eTransferSrc;
    }
    if (usage_flags & vk::ImageUsageFlagBits::eTransferDst) {
        feature_flags |= vk::FormatFeatureFlagBits2::eTransferDst;
    }
    if (usage_flags & vk::ImageUsageFlagBits::eSampled) {
        feature_flags |= vk::FormatFeatureFlagBits2::eSampledImage;
    }
    if (usage_flags & vk::ImageUsageFlagBits::eColorAttachment) {
        feature_flags |= vk::FormatFeatureFlagBits2::eColorAttachment;
    }
    if (usage_flags & vk::ImageUsageFlagBits::eDepthStencilAttachment) {
        feature_flags |= vk::FormatFeatureFlagBits2::eDepthStencilAttachment;
    }
    // Note: StorageImage is intentionally ignored for now since it is always set, and can mess up
    // compatibility checks.
    return feature_flags;
}

static vk::Result QueryImageSupport(const Vulkan::Instance& instance, vk::ImageType image_type,
                                    vk::Format format,
                                    vk::ImageUsageFlags usage, vk::ImageCreateFlags flags) {
    const vk::PhysicalDeviceImageFormatInfo2 query{
        .format = format,
        .type = image_type,
        .tiling = vk::ImageTiling::eOptimal,
        .usage = usage,
        .flags = flags,
    };
    auto result = instance.GetPhysicalDevice().getImageFormatProperties2(query);
    if (result.result == vk::Result::eErrorFormatNotSupported &&
        (usage & vk::ImageUsageFlagBits::eStorage)) {
        auto relaxed = query;
        relaxed.usage &= ~vk::ImageUsageFlagBits::eStorage;
        result = instance.GetPhysicalDevice().getImageFormatProperties2(relaxed);
    }
    return result.result;
}

bool ApplyBc1DCapabilityFallback(const Vulkan::Instance& instance, ImageInfo& info) {
    static const bool enabled = [] {
        const char* value = std::getenv("FL_BC_CAPABILITY_FALLBACK");
        return value && value[0] == '1';
    }();
    if (!enabled || info.props.is_bc_decoded || !info.props.is_block || info.props.is_depth ||
        info.props.is_volume || info.num_samples != 1 || info.type != AmdGpu::ImageType::Color1D ||
        AmdGpu::IsMacroTiled(info.array_mode)) {
        return false;
    }

    const vk::Format decoded = BcTinyDecodedFormat(info.pixel_format);
    if (decoded == vk::Format::eUndefined) {
        return false;
    }

    vk::ImageCreateFlags guest_flags{vk::ImageCreateFlagBits::eMutableFormat |
                                     vk::ImageCreateFlagBits::eExtendedUsage};
    if (instance.IsBlockTexelViewSupported()) {
        guest_flags |= vk::ImageCreateFlagBits::eBlockTexelViewCompatible;
    }
    const auto guest_usage = ImageUsageFlags(instance, info);
    const auto guest_format =
        instance.GetSupportedFormat(info.pixel_format, FormatFeatureFlags(guest_usage));
    const auto native_result =
        QueryImageSupport(instance, vk::ImageType::e1D, guest_format, guest_usage, guest_flags);

    if (native_result == vk::Result::eSuccess) {
        return false;
    }

    ImageInfo candidate = info;
    candidate.props.is_bc_decoded = true;
    candidate.props.is_host_2d = true;
    candidate.bc_format = info.pixel_format;
    candidate.pixel_format = decoded;

    const auto host_usage = ImageUsageFlags(instance, candidate);
    const auto host_format =
        instance.GetSupportedFormat(candidate.pixel_format, FormatFeatureFlags(host_usage));
    const vk::ImageCreateFlags host_flags{vk::ImageCreateFlagBits::eMutableFormat |
                                          vk::ImageCreateFlagBits::eExtendedUsage};
    const auto fallback_result =
        QueryImageSupport(instance, vk::ImageType::e2D, host_format, host_usage, host_flags);

    LOG_WARNING(Render_Vulkan,
                "BCN CAPABILITY: guest={:#x} {} 1D {}x{} layers={} native={} -> host={} 2D "
                "decoded={} fallback={}",
                info.guest_address, vk::to_string(info.pixel_format), info.size.width,
                info.size.height, info.resources.layers, vk::to_string(native_result),
                vk::to_string(host_format), vk::to_string(decoded), vk::to_string(fallback_result));

    if (fallback_result != vk::Result::eSuccess) {
        return false;
    }

    info = candidate;
    return true;
}

UniqueImage::~UniqueImage() {
    if (image) {
        vmaDestroyImage(allocator, image, allocation);
    }
}

void UniqueImage::Destroy() {
    if (image) {
        vmaDestroyImage(allocator, image, allocation);
        image = vk::Image{};
        allocation = {};
    }
}

void UniqueImage::Create(const vk::ImageCreateInfo& image_ci) {
    this->image_ci = image_ci;
    ASSERT(!image);
    const VmaAllocationCreateInfo alloc_ci = {
        .flags = VMA_ALLOCATION_CREATE_WITHIN_BUDGET_BIT,
        .usage = VMA_MEMORY_USAGE_AUTO_PREFER_DEVICE,
        .requiredFlags = 0,
        .preferredFlags = 0,
        .pool = VK_NULL_HANDLE,
        .pUserData = nullptr,
    };

    const VkImageCreateInfo image_ci_unsafe = static_cast<VkImageCreateInfo>(image_ci);
    VkImage unsafe_image{};
    VmaAllocationInfo alloc_info{};
    if (FlGpuResourceTrace()) {
        std::printf("GPU: FL resource vmaCreateImage begin type=%u format=%u extent=%ux%ux%u mips=%u layers=%u usage=0x%x flags=0x%x\\n",
                    image_ci_unsafe.imageType, image_ci_unsafe.format,
                    image_ci_unsafe.extent.width, image_ci_unsafe.extent.height,
                    image_ci_unsafe.extent.depth, image_ci_unsafe.mipLevels,
                    image_ci_unsafe.arrayLayers, image_ci_unsafe.usage, image_ci_unsafe.flags);
        std::fflush(stdout);
    }
    VkResult result = vmaCreateImage(allocator, &image_ci_unsafe, &alloc_ci, &unsafe_image,
                                     &allocation, &alloc_info);
    if (FlGpuResourceTrace()) {
        std::printf("GPU: FL resource vmaCreateImage end result=%d\\n", int(result));
        std::fflush(stdout);
    }
    if (result == VK_ERROR_OUT_OF_DEVICE_MEMORY) {
        // bbport: VRAM full (6 GB cards, high resolutions). Past the budget the driver keeps the
        // image in system memory, which is slower but no crash.
        Vulkan::NoteOverBudget("image");
        VmaAllocationCreateInfo over_budget = alloc_ci;
        over_budget.flags &= ~VMA_ALLOCATION_CREATE_WITHIN_BUDGET_BIT;
        result = vmaCreateImage(allocator, &image_ci_unsafe, &over_budget, &unsafe_image,
                                &allocation, &alloc_info);
    }
    ASSERT_MSG(result == VK_SUCCESS, "Failed allocating image with error {}",
               vk::to_string(vk::Result{result}));
    image = vk::Image{unsafe_image};
    size_bytes = alloc_info.size;
}

Image::Image(const Vulkan::Instance& instance, Vulkan::Runtime& runtime_,
             Common::SlotVector<ImageView>& slot_image_views_, const ImageInfo& info_)
    : guest_begin{info_.guest_address}, guest_end{info_.guest_address + info_.guest_size},
      info{info_}, runtime{&runtime_}, slot_image_views{&slot_image_views_} {
    BbStats::Timer timer{BbStats::t_image_create};
    if (info.pixel_format == vk::Format::eUndefined) {
        return;
    }

    image_uid = global_image_uid.Next();
    mip_hashes.resize(info.resources.levels);
    vk::ImageCreateFlags flags{vk::ImageCreateFlagBits::eMutableFormat |
                               vk::ImageCreateFlagBits::eExtendedUsage};
    if (info.props.is_volume) {
        flags |= vk::ImageCreateFlagBits::e2DArrayCompatible;
        if (instance.Is2dViewOf3dSupported()) {
            flags |= vk::ImageCreateFlagBits::e2DViewCompatibleEXT;
        }
    }
    if (info.props.is_block && !info.props.is_bc_decoded && instance.IsBlockTexelViewSupported()) {
        flags |= vk::ImageCreateFlagBits::eBlockTexelViewCompatible;
    }

    usage_flags = ImageUsageFlags(instance, info);
    format_features = FormatFeatureFlags(usage_flags);
    if (info.props.is_depth) {
        aspect_mask = vk::ImageAspectFlagBits::eDepth;
        if (info.props.has_stencil) {
            aspect_mask |= vk::ImageAspectFlagBits::eStencil;
        }
    }

    constexpr auto tiling = vk::ImageTiling::eOptimal;
    const auto supported_format = instance.GetSupportedFormat(info.pixel_format, format_features);
    vk::PhysicalDeviceImageFormatInfo2 format_info{
        .format = supported_format,
        .type = info.props.is_host_2d ? vk::ImageType::e2D : ConvertImageType(info.type),
        .tiling = tiling,
        .usage = usage_flags,
        .flags = flags,
    };
    if (FlGpuResourceTrace()) {
        std::printf("GPU: FL resource #%llu format-query begin guest=0x%llx guestType=%u hostType=%u format=%s size=%ux%ux%u mips=%u layers=%u host2d=%u decoded=%u usage=0x%x flags=0x%x\\n",
                    static_cast<unsigned long long>(image_uid),
                    static_cast<unsigned long long>(info.guest_address), u32(info.type),
                    u32(format_info.type), vk::to_string(supported_format).c_str(),
                    info.size.width, info.size.height, info.size.depth, info.resources.levels,
                    info.resources.layers, info.props.is_host_2d ? 1u : 0u,
                    info.props.is_bc_decoded ? 1u : 0u, u32(usage_flags), u32(flags));
        std::fflush(stdout);
    }
    auto image_format_properties =
        instance.GetPhysicalDevice().getImageFormatProperties2(format_info);
    if (FlGpuResourceTrace()) {
        std::printf("GPU: FL resource #%llu format-query end result=%s\\n",
                    static_cast<unsigned long long>(image_uid),
                    vk::to_string(image_format_properties.result).c_str());
        std::fflush(stdout);
    }
    // bbport: storage usage is speculative (see ImageUsageFlags). Drivers that refuse it for a
    // format (AMD's Windows driver for BC6H) get the image without it: creating the unsupported
    // combination anyway loses the device a few frames later.
    if (image_format_properties.result == vk::Result::eErrorFormatNotSupported &&
        (usage_flags & vk::ImageUsageFlagBits::eStorage)) {
        format_info.usage = usage_flags & ~vk::ImageUsageFlagBits::eStorage;
        image_format_properties = instance.GetPhysicalDevice().getImageFormatProperties2(format_info);
        if (image_format_properties.result == vk::Result::eSuccess) {
            usage_flags = format_info.usage;
            format_features = FormatFeatureFlags(usage_flags);
        } else {
            format_info.usage = usage_flags;
        }
    }
    if (image_format_properties.result != vk::Result::eSuccess) {
        const bool guest_bc_1d =
            info.props.is_block && !info.props.is_bc_decoded && !info.props.is_host_2d &&
            (info.type == AmdGpu::ImageType::Color1D ||
             info.type == AmdGpu::ImageType::Color1DArray);
        if (!guest_bc_1d) {
            LOG_ERROR(Render_Vulkan,
                      "Refusing unsupported image before vkCreateImage: format={} type={} flags={} "
                      "usage={} result={} guest={:#x} size={}x{}x{} layers={} mips={} decoded={}",
                      vk::to_string(supported_format), vk::to_string(format_info.type),
                      vk::to_string(format_info.flags), vk::to_string(format_info.usage),
                      vk::to_string(image_format_properties.result), info.guest_address,
                      info.size.width, info.size.height, info.size.depth, info.resources.layers,
                      info.resources.levels, info.props.is_bc_decoded);
            ASSERT_MSG(false, "Unsupported Vulkan image combination was blocked before driver call");
        }
        // Parity with the known-good First Light build: keep the resource genuinely 1D and let
        // vkCreateImage/VMA make the final decision. On the tested AMD driver this path advances
        // even though the format-properties query reports unsupported for BC1/BC4 1D.
        LOG_WARNING(Render_Vulkan,
                    "ELTUTZ PARITY: capability query rejected guest BC 1D but creation will be "
                    "attempted as 1D: guest={:#x} format={} type={} flags={} usage={}",
                    info.guest_address, vk::to_string(supported_format),
                    vk::to_string(format_info.type), vk::to_string(format_info.flags),
                    vk::to_string(format_info.usage));
        supported_samples = vk::SampleCountFlagBits::e1;
    } else {
        supported_samples = image_format_properties.value.imageFormatProperties.sampleCounts;
    }

    const vk::ImageCreateInfo image_ci = {
        .flags = flags,
        .imageType = info.props.is_host_2d ? vk::ImageType::e2D : ConvertImageType(info.type),
        .format = supported_format,
        .extent{
            .width = info.size.width,
            .height = info.size.height,
            .depth = info.size.depth,
        },
        .mipLevels = static_cast<u32>(info.resources.levels),
        .arrayLayers = static_cast<u32>(info.resources.layers),
        .samples = LiverpoolToVK::NumSamples(info.num_samples, supported_samples),
        .tiling = tiling,
        .usage = usage_flags,
        .initialLayout = vk::ImageLayout::eUndefined,
    };

    backing = &backing_images.emplace_back();
    backing->num_samples = info.num_samples;
    backing->image = UniqueImage{instance.GetDevice(), instance.GetAllocator()};
    if (FlGpuResourceTrace()) {
        std::printf("GPU: FL resource #%llu image-create begin\\n",
                    static_cast<unsigned long long>(image_uid));
        std::fflush(stdout);
    }
    backing->image.Create(image_ci);
    if (FlGpuResourceTrace()) {
        std::printf("GPU: FL resource #%llu image-create end\\n",
                    static_cast<unsigned long long>(image_uid));
        std::fflush(stdout);
    }

    Vulkan::SetObjectName(instance.GetDevice(), GetImage(),
                          "Image {}x{}x{} {} {} {:#x}:{:#x} L:{} M:{} S:{}", info.size.width,
                          info.size.height, info.size.depth, AmdGpu::NameOf(info.tile_mode),
                          vk::to_string(info.pixel_format), info.guest_address, info.guest_size,
                          info.resources.layers, info.resources.levels, info.num_samples);
}

Image::~Image() = default;

ImageView& Image::FindView(const ImageViewInfo& view_info, bool ensure_guest_samples) {
    if (ensure_guest_samples && backing->num_samples > 1 != info.num_samples > 1) {
        runtime->SetBackingSamples(this, info.num_samples);
    }
    const auto& view_infos = backing->image_view_infos;
    if (backing->last_view < view_infos.size() && view_infos[backing->last_view] == view_info) {
        return (*slot_image_views)[backing->image_view_ids[backing->last_view]];
    }
    const auto it = std::ranges::find(view_infos, view_info);
    if (it != view_infos.end()) {
        backing->last_view = static_cast<u32>(std::distance(view_infos.begin(), it));
        const auto view_id = backing->image_view_ids[backing->last_view];
        return (*slot_image_views)[view_id];
    }
    const auto view_id = slot_image_views->insert(runtime->GetInstance(), view_info, *this);
    backing->image_view_infos.emplace_back(view_info);
    backing->image_view_ids.emplace_back(view_id);
    return (*slot_image_views)[view_id];
}

void Image::GetBarriers(Barriers& barriers, vk::ImageLayout dst_layout, vk::AccessFlags2 dst_mask,
                        vk::PipelineStageFlags2 dst_stage,
                        std::optional<SubresourceRange> subres_range) {
    auto& last_state = backing->state;
    auto& subresource_states = backing->subresource_states;

    const bool needs_partial_transition =
        subres_range &&
        (subres_range->base != SubresourceBase{} || subres_range->extent != info.resources);
    const bool partially_transited = !subresource_states.empty();

    if (needs_partial_transition || partially_transited) {
        if (!partially_transited) {
            subresource_states.resize(info.resources.levels * info.resources.layers);
            std::fill(subresource_states.begin(), subresource_states.end(), last_state);
        }

        // In case of partial transition, we need to change the specified subresources only.
        // Otherwise all subresources need to be set to the same state so we can use a full
        // resource transition for the next time.
        const auto mips =
            needs_partial_transition
                ? std::ranges::views::iota(subres_range->base.level,
                                           subres_range->base.level + subres_range->extent.levels)
                : std::views::iota(0u, info.resources.levels);
        const auto layers =
            needs_partial_transition
                ? std::ranges::views::iota(subres_range->base.layer,
                                           subres_range->base.layer + subres_range->extent.layers)
                : std::views::iota(0u, info.resources.layers);

        for (u32 mip : mips) {
            for (u32 layer : layers) {
                // NOTE: these loops may produce a lot of small barriers.
                // If this becomes a problem, we can optimize it by merging adjacent barriers.
                const auto subres_idx = mip * info.resources.layers + layer;
                ASSERT(subres_idx < subresource_states.size());
                auto& state = subresource_states[subres_idx];

                constexpr auto write_flags = vk::AccessFlagBits2::eTransferWrite |
                                             vk::AccessFlagBits2::eShaderWrite |
                                             vk::AccessFlagBits2::eMemoryWrite;
                const bool is_write = static_cast<bool>(state.access_mask & write_flags);
                if (state.layout != dst_layout || state.access_mask != dst_mask || is_write) {
                    barriers.emplace_back(vk::ImageMemoryBarrier2{
                        .srcStageMask = state.pl_stage,
                        .srcAccessMask = state.access_mask,
                        .dstStageMask = dst_stage,
                        .dstAccessMask = dst_mask,
                        .oldLayout = state.layout,
                        .newLayout = dst_layout,
                        .srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED,
                        .dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED,
                        .image = GetImage(),
                        .subresourceRange{
                            .aspectMask = aspect_mask,
                            .baseMipLevel = mip,
                            .levelCount = 1,
                            .baseArrayLayer = layer,
                            .layerCount = 1,
                        },
                    });
                    state.layout = dst_layout;
                    state.access_mask = dst_mask;
                    state.pl_stage = dst_stage;
                }
            }
        }

        if (!needs_partial_transition) {
            subresource_states.clear();
        }
    } else { // Full resource transition
        constexpr auto write_flags = vk::AccessFlagBits2::eTransferWrite |
                                     vk::AccessFlagBits2::eShaderWrite |
                                     vk::AccessFlagBits2::eMemoryWrite;
        const bool is_write = static_cast<bool>(last_state.access_mask & write_flags);
        if (last_state.layout == dst_layout && last_state.access_mask == dst_mask && !is_write) {
            return;
        }

        barriers.emplace_back(vk::ImageMemoryBarrier2{
            .srcStageMask = last_state.pl_stage,
            .srcAccessMask = last_state.access_mask,
            .dstStageMask = dst_stage,
            .dstAccessMask = dst_mask,
            .oldLayout = last_state.layout,
            .newLayout = dst_layout,
            .srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED,
            .dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED,
            .image = GetImage(),
            .subresourceRange{
                .aspectMask = aspect_mask,
                .baseMipLevel = 0,
                .levelCount = VK_REMAINING_MIP_LEVELS,
                .baseArrayLayer = 0,
                .layerCount = VK_REMAINING_ARRAY_LAYERS,
            },
        });
    }

    last_state.layout = dst_layout;
    last_state.access_mask = dst_mask;
    last_state.pl_stage = dst_stage;
}

} // namespace VideoCore
