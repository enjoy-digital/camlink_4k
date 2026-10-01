/*
 * This file is part of CamLinX.
 *
 * Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
 * SPDX-License-Identifier: BSD-2-Clause
 *
 * Direct display output with Vulkan (see vk_out.h).
 */

#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <math.h>

#include <X11/Xlib.h>
#include <X11/extensions/Xrandr.h>
#define VK_USE_PLATFORM_XLIB_XRANDR_EXT
#include <vulkan/vulkan.h>

#include "vk_out.h"

struct vk_out {
    Display         *dpy;
    VkInstance       inst;
    VkPhysicalDevice pd;
    VkDevice         dev;
    VkQueue          queue;
    VkDisplayKHR     display;
    VkSurfaceKHR     surface;
    VkSwapchainKHR   swapchain;
    VkImage          images[8];
    uint32_t         nimages;
    int              shared;          /* Shared continuous refresh (front buffer) mode.        */
    VkImage          frame;           /* Immediate mode: device local copy of the frame.       */
    VkDeviceMemory   frame_mem;
    VkBuffer         staging;         /* Host visible BGRA frame (rows converted by the CPU).  */
    VkDeviceMemory   staging_mem;
    uint8_t         *staging_map;
    VkCommandPool    pool;
    VkCommandBuffer  cmd;
    VkFence          fence;
    VkSemaphore      acquired;
    int              width, height;
    double           period_ms, line_ms;

    /* First pixel out (start of the active scan) timestamps, from a display event thread. */
    pthread_t        vblank_thread;
    pthread_mutex_t  lock;
    double           vblank_ms;
    volatile int     running;
};

static double now_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec*1e3 + ts.tv_nsec*1e-6;
}

#define VKF(o, name) ((PFN_##name)vkGetInstanceProcAddr((o)->inst, #name))
#define CHECK(x) do { VkResult _r = (x); if (_r != VK_SUCCESS) { fprintf(stderr, "vk: %s: %d\n", #x, _r); return NULL; } } while (0)

static uint32_t mem_type(struct vk_out *o, uint32_t bits, VkMemoryPropertyFlags flags)
{
    VkPhysicalDeviceMemoryProperties mp;
    vkGetPhysicalDeviceMemoryProperties(o->pd, &mp);
    for (uint32_t i = 0; i < mp.memoryTypeCount; i++)
        if ((bits & (1u << i)) && (mp.memoryTypes[i].propertyFlags & flags) == flags)
            return i;
    return 0;
}

static void barrier(VkCommandBuffer cmd, VkImage img, VkImageLayout from, VkImageLayout to)
{
    VkImageMemoryBarrier b = {
        .sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER,
        .srcAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT | VK_ACCESS_MEMORY_READ_BIT,
        .dstAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT | VK_ACCESS_TRANSFER_READ_BIT | VK_ACCESS_MEMORY_READ_BIT,
        .oldLayout = from, .newLayout = to,
        .srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED, .dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED,
        .image = img, .subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1},
    };
    vkCmdPipelineBarrier(cmd, VK_PIPELINE_STAGE_ALL_COMMANDS_BIT, VK_PIPELINE_STAGE_ALL_COMMANDS_BIT, 0, 0, NULL, 0, NULL, 1, &b);
}

/* Display events: a fence signaled at each first pixel out, timestamped on wake up. */
static void *vblank_thread(void *arg)
{
    struct vk_out *o = arg;
    PFN_vkRegisterDisplayEventEXT reg = (PFN_vkRegisterDisplayEventEXT)vkGetDeviceProcAddr(o->dev, "vkRegisterDisplayEventEXT");
    while (o->running) {
        VkDisplayEventInfoEXT ev = {.sType = VK_STRUCTURE_TYPE_DISPLAY_EVENT_INFO_EXT, .displayEvent = VK_DISPLAY_EVENT_TYPE_FIRST_PIXEL_OUT_EXT};
        VkFence f;
        if (reg(o->dev, o->display, &ev, NULL, &f) != VK_SUCCESS)
            break;
        if (vkWaitForFences(o->dev, 1, &f, VK_TRUE, 100000000ULL) == VK_SUCCESS) {
            double t = now_ms();
            pthread_mutex_lock(&o->lock);
            o->vblank_ms = t;
            pthread_mutex_unlock(&o->lock);
        }
        vkDestroyFence(o->dev, f, NULL);
    }
    return NULL;
}

/* Mode timing (lines per frame) from RandR: the Vulkan display mode only has the refresh rate. */
static int randr_vtotal(Display *dpy, RROutput out, int width, int height, double hz)
{
    XRRScreenResources *res = XRRGetScreenResourcesCurrent(dpy, DefaultRootWindow(dpy));
    XRROutputInfo *oi = XRRGetOutputInfo(dpy, res, out);
    int vtotal = 0;
    double best = 1e9;
    for (int i = 0; i < oi->nmode; i++)
        for (int m = 0; m < res->nmode; m++) {
            XRRModeInfo *mi = &res->modes[m];
            if (mi->id != oi->modes[i] || (int)mi->width != width || (int)mi->height != height || !mi->hTotal || !mi->vTotal)
                continue;
            double d = fabs((double)mi->dotClock/((double)mi->hTotal*mi->vTotal) - hz);
            if (d < best) { best = d; vtotal = mi->vTotal; }
        }
    XRRFreeOutputInfo(oi);
    XRRFreeScreenResources(res);
    return vtotal ? vtotal : height*1125/1080;
}

struct vk_out *vk_out_open(const char *output, int width, int height, double refresh)
{
    struct vk_out *o = calloc(1, sizeof(*o));
    o->dpy = XOpenDisplay(NULL);
    if (!o->dpy) { fprintf(stderr, "vk: no X display.\n"); return NULL; }
    XRRScreenResources *res = XRRGetScreenResourcesCurrent(o->dpy, DefaultRootWindow(o->dpy));
    RROutput out = 0;
    for (int i = 0; i < res->noutput; i++) {
        XRROutputInfo *oi = XRRGetOutputInfo(o->dpy, res, res->outputs[i]);
        if (!strcmp(oi->name, output)) {
            out = res->outputs[i];
            if (oi->crtc)
                fprintf(stderr, "vk: %s is in use by the desktop (xrandr --output %s --off --set non-desktop 1).\n", output, output);
        }
        XRRFreeOutputInfo(oi);
    }
    XRRFreeScreenResources(res);
    if (!out) { fprintf(stderr, "vk: output %s not found.\n", output); return NULL; }

    /* Instance, physical device, display acquired from X. */
    const char *iext[] = {"VK_KHR_surface", "VK_KHR_display", "VK_EXT_direct_mode_display", "VK_EXT_acquire_xlib_display",
        "VK_KHR_get_surface_capabilities2", "VK_EXT_display_surface_counter"};
    VkApplicationInfo app = {.sType = VK_STRUCTURE_TYPE_APPLICATION_INFO, .pApplicationName = "camlinx_view", .apiVersion = VK_API_VERSION_1_1};
    VkInstanceCreateInfo ici = {.sType = VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO, .pApplicationInfo = &app,
        .enabledExtensionCount = 6, .ppEnabledExtensionNames = iext};
    CHECK(vkCreateInstance(&ici, NULL, &o->inst));
    uint32_t n = 1;
    vkEnumeratePhysicalDevices(o->inst, &n, &o->pd);
    CHECK(VKF(o, vkGetRandROutputDisplayEXT)(o->pd, o->dpy, out, &o->display));
    CHECK(VKF(o, vkAcquireXlibDisplayEXT)(o->pd, o->dpy, o->display));

    /* Mode and display plane. */
    vkGetDisplayModePropertiesKHR(o->pd, o->display, &n, NULL);
    VkDisplayModePropertiesKHR *modes = calloc(n, sizeof(*modes));
    vkGetDisplayModePropertiesKHR(o->pd, o->display, &n, modes);
    VkDisplayModePropertiesKHR *mode = NULL; /* Requested refresh (closest), else the highest. */
    for (uint32_t i = 0; i < n; i++) {
        VkDisplayModePropertiesKHR *m = &modes[i];
        if ((int)m->parameters.visibleRegion.width != width || (int)m->parameters.visibleRegion.height != height)
            continue;
        if (!mode || (refresh > 0 ? fabs(m->parameters.refreshRate/1e3 - refresh) < fabs(mode->parameters.refreshRate/1e3 - refresh)
                                  : m->parameters.refreshRate > mode->parameters.refreshRate))
            mode = m;
    }
    if (!mode) { fprintf(stderr, "vk: no %dx%d mode on %s.\n", width, height, output); return NULL; }
    o->width     = width;
    o->height    = height;
    o->period_ms = 1e6/mode->parameters.refreshRate;
    o->line_ms   = o->period_ms/randr_vtotal(o->dpy, out, width, height, 1e3/o->period_ms);

    uint32_t nplanes = 0, plane = UINT32_MAX, stack = 0;
    vkGetPhysicalDeviceDisplayPlanePropertiesKHR(o->pd, &nplanes, NULL);
    VkDisplayPlanePropertiesKHR *planes = calloc(nplanes, sizeof(*planes));
    vkGetPhysicalDeviceDisplayPlanePropertiesKHR(o->pd, &nplanes, planes);
    for (uint32_t p = 0; p < nplanes && plane == UINT32_MAX; p++) {
        uint32_t nd = 0;
        vkGetDisplayPlaneSupportedDisplaysKHR(o->pd, p, &nd, NULL);
        VkDisplayKHR *ds = calloc(nd, sizeof(*ds));
        vkGetDisplayPlaneSupportedDisplaysKHR(o->pd, p, &nd, ds);
        for (uint32_t i = 0; i < nd; i++)
            if (ds[i] == o->display) { plane = p; stack = planes[p].currentStackIndex; }
        free(ds);
    }
    if (plane == UINT32_MAX) { fprintf(stderr, "vk: no display plane for %s.\n", output); return NULL; }
    VkDisplaySurfaceCreateInfoKHR sci = {
        .sType = VK_STRUCTURE_TYPE_DISPLAY_SURFACE_CREATE_INFO_KHR, .displayMode = mode->displayMode,
        .planeIndex = plane, .planeStackIndex = stack, .transform = VK_SURFACE_TRANSFORM_IDENTITY_BIT_KHR,
        .globalAlpha = 1.0f, .alphaMode = VK_DISPLAY_PLANE_ALPHA_OPAQUE_BIT_KHR,
        .imageExtent = mode->parameters.visibleRegion,
    };
    CHECK(vkCreateDisplayPlaneSurfaceKHR(o->inst, &sci, NULL, &o->surface));

    /* Device: one queue (graphics/transfer + present), swapchain (+ shared presentable image). */
    uint32_t nq = 0, qf = UINT32_MAX;
    vkGetPhysicalDeviceQueueFamilyProperties(o->pd, &nq, NULL);
    VkQueueFamilyProperties *qp = calloc(nq, sizeof(*qp));
    vkGetPhysicalDeviceQueueFamilyProperties(o->pd, &nq, qp);
    for (uint32_t i = 0; i < nq && qf == UINT32_MAX; i++) {
        VkBool32 present = VK_FALSE;
        vkGetPhysicalDeviceSurfaceSupportKHR(o->pd, i, o->surface, &present);
        if (present && (qp[i].queueFlags & VK_QUEUE_GRAPHICS_BIT))
            qf = i;
    }
    uint32_t npm = 0;
    vkGetPhysicalDeviceSurfacePresentModesKHR(o->pd, o->surface, &npm, NULL);
    VkPresentModeKHR *pms = calloc(npm, sizeof(*pms));
    vkGetPhysicalDeviceSurfacePresentModesKHR(o->pd, o->surface, &npm, pms);
    int has_shared = 0, has_immediate = 0;
    for (uint32_t i = 0; i < npm; i++) {
        has_shared    |= pms[i] == VK_PRESENT_MODE_SHARED_CONTINUOUS_REFRESH_KHR;
        has_immediate |= pms[i] == VK_PRESENT_MODE_IMMEDIATE_KHR;
    }
    if (getenv("CAMLINX_VK_NO_SHARED"))
        has_shared = 0;
    o->shared = has_shared;
    float prio = 1.0f;
    VkDeviceQueueCreateInfo qci = {.sType = VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO, .queueFamilyIndex = qf, .queueCount = 1, .pQueuePriorities = &prio};
    const char *dext[] = {"VK_KHR_swapchain", "VK_EXT_display_control", "VK_KHR_shared_presentable_image"};
    VkDeviceCreateInfo dci = {.sType = VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO, .queueCreateInfoCount = 1, .pQueueCreateInfos = &qci,
        .enabledExtensionCount = o->shared ? 3 : 2, .ppEnabledExtensionNames = dext};
    CHECK(vkCreateDevice(o->pd, &dci, NULL, &o->dev));
    vkGetDeviceQueue(o->dev, qf, 0, &o->queue);

    VkSurfaceCapabilitiesKHR caps;
    vkGetPhysicalDeviceSurfaceCapabilitiesKHR(o->pd, o->surface, &caps);
    VkSwapchainCreateInfoKHR swci = {
        .sType = VK_STRUCTURE_TYPE_SWAPCHAIN_CREATE_INFO_KHR, .surface = o->surface,
        .minImageCount = o->shared ? 1 : (caps.minImageCount > 2 ? caps.minImageCount : 2),
        .imageFormat = VK_FORMAT_B8G8R8A8_UNORM, .imageColorSpace = VK_COLOR_SPACE_SRGB_NONLINEAR_KHR,
        .imageExtent = mode->parameters.visibleRegion, .imageArrayLayers = 1,
        .imageUsage = VK_IMAGE_USAGE_TRANSFER_DST_BIT, .imageSharingMode = VK_SHARING_MODE_EXCLUSIVE,
        .preTransform = VK_SURFACE_TRANSFORM_IDENTITY_BIT_KHR, .compositeAlpha = VK_COMPOSITE_ALPHA_OPAQUE_BIT_KHR,
        .presentMode = o->shared ? VK_PRESENT_MODE_SHARED_CONTINUOUS_REFRESH_KHR : has_immediate ? VK_PRESENT_MODE_IMMEDIATE_KHR : VK_PRESENT_MODE_FIFO_KHR,
        .clipped = VK_TRUE,
    };
    CHECK(vkCreateSwapchainKHR(o->dev, &swci, NULL, &o->swapchain));
    o->nimages = 8;
    vkGetSwapchainImagesKHR(o->dev, o->swapchain, &o->nimages, o->images);

    /* Staging buffer (host visible, BGRA frame), command buffer, sync. */
    VkBufferCreateInfo bci = {.sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO, .size = (VkDeviceSize)width*height*4,
        .usage = VK_BUFFER_USAGE_TRANSFER_SRC_BIT};
    CHECK(vkCreateBuffer(o->dev, &bci, NULL, &o->staging));
    VkMemoryRequirements mr;
    vkGetBufferMemoryRequirements(o->dev, o->staging, &mr);
    VkMemoryAllocateInfo mai = {.sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO, .allocationSize = mr.size,
        .memoryTypeIndex = mem_type(o, mr.memoryTypeBits, VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT)};
    CHECK(vkAllocateMemory(o->dev, &mai, NULL, &o->staging_mem));
    vkBindBufferMemory(o->dev, o->staging, o->staging_mem, 0);
    vkMapMemory(o->dev, o->staging_mem, 0, VK_WHOLE_SIZE, 0, (void **)&o->staging_map);
    memset(o->staging_map, 0, (size_t)width*height*4);
    if (!o->shared) {
        VkImageCreateInfo ii = {.sType = VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO, .imageType = VK_IMAGE_TYPE_2D,
            .format = VK_FORMAT_B8G8R8A8_UNORM, .extent = {width, height, 1}, .mipLevels = 1, .arrayLayers = 1,
            .samples = VK_SAMPLE_COUNT_1_BIT, .tiling = VK_IMAGE_TILING_OPTIMAL,
            .usage = VK_IMAGE_USAGE_TRANSFER_SRC_BIT | VK_IMAGE_USAGE_TRANSFER_DST_BIT};
        CHECK(vkCreateImage(o->dev, &ii, NULL, &o->frame));
        vkGetImageMemoryRequirements(o->dev, o->frame, &mr);
        mai.allocationSize  = mr.size;
        mai.memoryTypeIndex = mem_type(o, mr.memoryTypeBits, VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT);
        CHECK(vkAllocateMemory(o->dev, &mai, NULL, &o->frame_mem));
        vkBindImageMemory(o->dev, o->frame, o->frame_mem, 0);
    }
    VkCommandPoolCreateInfo pci = {.sType = VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO,
        .flags = VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT, .queueFamilyIndex = qf};
    CHECK(vkCreateCommandPool(o->dev, &pci, NULL, &o->pool));
    VkCommandBufferAllocateInfo cai = {.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO, .commandPool = o->pool,
        .level = VK_COMMAND_BUFFER_LEVEL_PRIMARY, .commandBufferCount = 1};
    CHECK(vkAllocateCommandBuffers(o->dev, &cai, &o->cmd));
    VkFenceCreateInfo fci = {.sType = VK_STRUCTURE_TYPE_FENCE_CREATE_INFO};
    CHECK(vkCreateFence(o->dev, &fci, NULL, &o->fence));
    VkSemaphoreCreateInfo smi = {.sType = VK_STRUCTURE_TYPE_SEMAPHORE_CREATE_INFO};
    CHECK(vkCreateSemaphore(o->dev, &smi, NULL, &o->acquired));

    /* Initial state: images cleared (black); shared mode: the image acquired and presented once,
     * then scanned continuously while we write into it. */
    VkCommandBufferBeginInfo cbi = {.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO};
    VkClearColorValue black = {{0, 0, 0, 1}};
    VkImageSubresourceRange rng = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1};
    uint32_t idx = 0;
    if (o->shared)
        CHECK(vkAcquireNextImageKHR(o->dev, o->swapchain, UINT64_MAX, o->acquired, VK_NULL_HANDLE, &idx));
    vkBeginCommandBuffer(o->cmd, &cbi);
    if (o->shared) {
        barrier(o->cmd, o->images[0], VK_IMAGE_LAYOUT_UNDEFINED, VK_IMAGE_LAYOUT_SHARED_PRESENT_KHR);
        vkCmdClearColorImage(o->cmd, o->images[0], VK_IMAGE_LAYOUT_SHARED_PRESENT_KHR, &black, 1, &rng);
    } else {
        barrier(o->cmd, o->frame, VK_IMAGE_LAYOUT_UNDEFINED, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL);
        vkCmdClearColorImage(o->cmd, o->frame, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, &black, 1, &rng);
    }
    vkEndCommandBuffer(o->cmd);
    VkPipelineStageFlags stage = VK_PIPELINE_STAGE_ALL_COMMANDS_BIT;
    VkSubmitInfo si = {.sType = VK_STRUCTURE_TYPE_SUBMIT_INFO, .commandBufferCount = 1, .pCommandBuffers = &o->cmd,
        .waitSemaphoreCount = o->shared ? 1 : 0, .pWaitSemaphores = &o->acquired, .pWaitDstStageMask = &stage};
    CHECK(vkQueueSubmit(o->queue, 1, &si, o->fence));
    vkWaitForFences(o->dev, 1, &o->fence, VK_TRUE, UINT64_MAX);
    vkResetFences(o->dev, 1, &o->fence);
    if (o->shared) {
        VkPresentInfoKHR pi = {.sType = VK_STRUCTURE_TYPE_PRESENT_INFO_KHR, .swapchainCount = 1, .pSwapchains = &o->swapchain, .pImageIndices = &idx};
        CHECK(vkQueuePresentKHR(o->queue, &pi));
    }

    pthread_mutex_init(&o->lock, NULL);
    o->running = 1;
    pthread_create(&o->vblank_thread, NULL, vblank_thread, o);
    printf("vk: %s acquired, %dx%d@%.2f Hz (%d lines), %s present, direct display\n", output, width, height,
        1e3/o->period_ms, (int)(o->period_ms/o->line_ms + 0.5), vk_out_mode(o));
    free(modes); free(planes); free(qp); free(pms);
    return o;
}

const char *vk_out_mode(struct vk_out *o)
{
    return o->shared ? "shared (front buffer)" : "immediate";
}

void vk_out_write_yuy2(struct vk_out *o, const uint8_t *yuy2, int src_width, int r0, int r1)
{
    int w = src_width < o->width ? src_width : o->width;
    if (r1 > o->height) r1 = o->height;
    if (r1 <= r0) return;
    /* YUY2 -> BGRA (BT.709 limited range) into the staging frame. */
    for (int y = r0; y < r1; y++) {
        const uint8_t *s = yuy2 + (size_t)y*src_width*2;
        uint8_t *d = o->staging_map + (size_t)y*o->width*4;
        for (int x = 0; x + 1 < w; x += 2, s += 4, d += 8) {
            int u = s[1] - 128, v = s[3] - 128;
            int ru = 459*v, gu = -55*u - 136*v, bu = 541*u;
            for (int k = 0; k < 2; k++) {
                int c = 298*(s[2*k] - 16) + 128, r = (c + ru) >> 8, g = (c + gu) >> 8, b = (c + bu) >> 8;
                d[4*k + 0] = b < 0 ? 0 : b > 255 ? 255 : b;
                d[4*k + 1] = g < 0 ? 0 : g > 255 ? 255 : g;
                d[4*k + 2] = r < 0 ? 0 : r > 255 ? 255 : r;
                d[4*k + 3] = 255;
            }
        }
    }
    /* Rows -> display: shared mode straight into the scanned image, immediate mode into the frame
     * copy then a full image copy and an immediate present. */
    VkBufferImageCopy region = {.bufferOffset = (VkDeviceSize)r0*o->width*4, .bufferRowLength = o->width,
        .imageSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1}, .imageOffset = {0, r0, 0}, .imageExtent = {o->width, r1 - r0, 1}};
    VkCommandBufferBeginInfo cbi = {.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO, .flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT};
    uint32_t idx = 0;
    if (!o->shared && vkAcquireNextImageKHR(o->dev, o->swapchain, UINT64_MAX, o->acquired, VK_NULL_HANDLE, &idx) != VK_SUCCESS)
        return;
    vkResetCommandBuffer(o->cmd, 0);
    vkBeginCommandBuffer(o->cmd, &cbi);
    if (o->shared) {
        vkCmdCopyBufferToImage(o->cmd, o->staging, o->images[0], VK_IMAGE_LAYOUT_SHARED_PRESENT_KHR, 1, &region);
    } else {
        vkCmdCopyBufferToImage(o->cmd, o->staging, o->frame, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 1, &region);
        barrier(o->cmd, o->frame, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL);
        barrier(o->cmd, o->images[idx], VK_IMAGE_LAYOUT_UNDEFINED, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL);
        VkImageCopy ic = {.srcSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1}, .dstSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1},
            .extent = {o->width, o->height, 1}};
        vkCmdCopyImage(o->cmd, o->frame, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, o->images[idx], VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 1, &ic);
        barrier(o->cmd, o->images[idx], VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, VK_IMAGE_LAYOUT_PRESENT_SRC_KHR);
        barrier(o->cmd, o->frame, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL);
    }
    vkEndCommandBuffer(o->cmd);
    VkPipelineStageFlags stage = VK_PIPELINE_STAGE_ALL_COMMANDS_BIT;
    VkSubmitInfo si = {.sType = VK_STRUCTURE_TYPE_SUBMIT_INFO, .commandBufferCount = 1, .pCommandBuffers = &o->cmd,
        .waitSemaphoreCount = o->shared ? 0 : 1, .pWaitSemaphores = &o->acquired, .pWaitDstStageMask = &stage};
    vkQueueSubmit(o->queue, 1, &si, o->fence);
    vkWaitForFences(o->dev, 1, &o->fence, VK_TRUE, UINT64_MAX);
    vkResetFences(o->dev, 1, &o->fence);
    if (!o->shared) {
        VkPresentInfoKHR pi = {.sType = VK_STRUCTURE_TYPE_PRESENT_INFO_KHR, .swapchainCount = 1, .pSwapchains = &o->swapchain, .pImageIndices = &idx};
        vkQueuePresentKHR(o->queue, &pi);
    }
}

double vk_out_scanout_ms(struct vk_out *o, int row, double t_ms)
{
    pthread_mutex_lock(&o->lock);
    double vb = o->vblank_ms;
    pthread_mutex_unlock(&o->lock);
    if (vb <= 0)
        return -1;
    double t = vb + row*o->line_ms; /* First pixel out = first active line. */
    while (t < t_ms)
        t += o->period_ms;
    while (t - o->period_ms >= t_ms)
        t -= o->period_ms;
    return t;
}

void vk_out_close(struct vk_out *o)
{
    if (!o) return;
    o->running = 0;
    pthread_join(o->vblank_thread, NULL);
    vkDeviceWaitIdle(o->dev);
    vkDestroySwapchainKHR(o->dev, o->swapchain, NULL);
    vkDestroyDevice(o->dev, NULL);
    vkDestroySurfaceKHR(o->inst, o->surface, NULL);
    VKF(o, vkReleaseDisplayEXT)(o->pd, o->display); /* The X server gets the output back. */
    vkDestroyInstance(o->inst, NULL);
    XCloseDisplay(o->dpy);
    free(o);
}
