/* Small native hotfix module for First Light.
 *
 * Keep title-specific/simple ABI contracts here instead of growing bb-probe.exe.
 * The host loads this DLL dynamically and asks it first for overrides.
 */
#include <stdint.h>
#include <stddef.h>
#include <string.h>

#if defined(_WIN32)
#define EXPORT __declspec(dllexport)
#else
#define EXPORT __attribute__((visibility("default")))
#endif
#define ABI __attribute__((sysv_abi))

static int same_nid(const char *a,const char *b) {
    const char *as=a ? strchr(a,'#') : NULL;
    const char *bs=b ? strchr(b,'#') : NULL;
    size_t an=a ? (as ? (size_t)(as-a) : strlen(a)) : 0;
    size_t bn=b ? (bs ? (size_t)(bs-b) : strlen(b)) : 0;
    return a && b && an==bn && !memcmp(a,b,an);
}

static ABI int32_t is_neo_mode(void) { return 0; }
static ABI int32_t has_neo_mode(void) { return 0; }

static ABI int32_t disc_map_request(const char *path,int64_t offset,int64_t bytes,
                                    int32_t *flags,int32_t *ret1,int32_t *ret2) {
    (void)path; (void)offset; (void)bytes;
    if (flags) *flags=0;
    if (ret1) *ret1=0;
    if (ret2) *ret2=0;
    return 0;
}

EXPORT uintptr_t fl_runtime_resolve_v1(const char *nid,const char *symbol) {
    if ((symbol && !strcmp(symbol,"sceKernelIsNeoMode")) || same_nid(nid,"WslcK1FQcGI"))
        return (uintptr_t)is_neo_mode;
    if ((symbol && !strcmp(symbol,"sceKernelHasNeoMode")) || same_nid(nid,"rNRtm1uioyY"))
        return (uintptr_t)has_neo_mode;
    if ((symbol && !strcmp(symbol,"sceDiscMap_7C980FFB0AA27E7A")) || same_nid(nid,"fJgP+wqifno"))
        return (uintptr_t)disc_map_request;
    return 0;
}

EXPORT void fl_runtime_report_v1(void) {
    /* Reserved for module-specific counters. */
}
